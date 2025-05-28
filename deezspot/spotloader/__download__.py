import traceback
import json
import os 
import time
import signal
import atexit
import sys
from copy import deepcopy
from os.path import isfile, dirname
from librespot.core import Session
from deezspot.exceptions import TrackNotFound
from librespot.metadata import TrackId, EpisodeId
from deezspot.spotloader.spotify_settings import qualities
from deezspot.libutils.others_settings import answers
from deezspot.__taggers__ import write_tags, check_track
from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality
from deezspot.libutils.audio_converter import convert_audio, parse_format_string
from os import (
    remove,
    system,
    replace as os_replace,
)
from deezspot.models import (
    Track,
    Album,
    Playlist,
    Preferences,
    Episode,
)
from deezspot.libutils.utils import (
    set_path,
    create_zip,
    request,
)
from mutagen import File
from mutagen.easyid3 import EasyID3
from mutagen.oggvorbis import OggVorbis
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from deezspot.libutils.logging_utils import logger

# --- Global retry counter variables ---
GLOBAL_RETRY_COUNT = 0
GLOBAL_MAX_RETRIES = 100  # Adjust this value as needed

# --- Global tracking of active downloads ---
ACTIVE_DOWNLOADS = set()
CLEANUP_LOCK = False
CURRENT_DOWNLOAD = None

def register_active_download(file_path):
    """Register a file as being actively downloaded"""
    global CURRENT_DOWNLOAD
    ACTIVE_DOWNLOADS.add(file_path)
    CURRENT_DOWNLOAD = file_path
    
def unregister_active_download(file_path):
    """Remove a file from the active downloads list"""
    global CURRENT_DOWNLOAD
    if file_path in ACTIVE_DOWNLOADS:
        ACTIVE_DOWNLOADS.remove(file_path)
        if CURRENT_DOWNLOAD == file_path:
            CURRENT_DOWNLOAD = None

def cleanup_active_downloads():
    """Clean up any incomplete downloads during process termination"""
    global CLEANUP_LOCK, CURRENT_DOWNLOAD
    if CLEANUP_LOCK:
        return
        
    CLEANUP_LOCK = True
    # Only remove the file that was in progress when stopped
    if CURRENT_DOWNLOAD:
        try:
            if os.path.exists(CURRENT_DOWNLOAD):
                logger.info(f"Removing incomplete download: {CURRENT_DOWNLOAD}")
                os.remove(CURRENT_DOWNLOAD)
                unregister_active_download(CURRENT_DOWNLOAD)
        except Exception as e:
            logger.error(f"Error cleaning up file {CURRENT_DOWNLOAD}: {str(e)}")
    CLEANUP_LOCK = False

# Register the cleanup function to run on exit
atexit.register(cleanup_active_downloads)

# Set up signal handlers
def signal_handler(sig, frame):
    logger.info(f"Received termination signal {sig}. Cleaning up...")
    cleanup_active_downloads()
    if sig == signal.SIGINT:
        logger.info("CTRL+C received. Exiting...")
    sys.exit(0)

# Register signal handlers for common termination signals
signal.signal(signal.SIGINT, signal_handler)   # CTRL+C
signal.signal(signal.SIGTERM, signal_handler)  # Normal termination
try:
    # These may not be available on all platforms
    signal.signal(signal.SIGHUP, signal_handler)   # Terminal closed
    signal.signal(signal.SIGQUIT, signal_handler)  # CTRL+\
except AttributeError:
    pass

class Download_JOB:
    session = None
    progress_reporter = None

    @classmethod
    def __init__(cls, session: Session) -> None:
        cls.session = session

    @classmethod
    def set_progress_reporter(cls, reporter):
        cls.progress_reporter = reporter
        
    @classmethod
    def report_progress(cls, progress_data):
        """Report progress if a reporter is configured."""
        if cls.progress_reporter:
            cls.progress_reporter.report(progress_data)
        else:
            # Fallback to logger if no reporter is configured
            logger.info(json.dumps(progress_data))

class EASY_DW:
    def __init__(
        self,
        preferences: Preferences,
        parent: str = None  # Can be 'album', 'playlist', or None for individual track
    ) -> None:
        
        self.__preferences = preferences
        self.__parent = parent  # Store the parent type

        self.__ids = preferences.ids
        self.__link = preferences.link
        self.__output_dir = preferences.output_dir
        self.__method_save = preferences.method_save
        self.__song_metadata = preferences.song_metadata
        self.__not_interface = preferences.not_interface
        self.__quality_download = preferences.quality_download or "NORMAL"
        self.__recursive_download = preferences.recursive_download
        self.__type = "episode" if preferences.is_episode else "track"  # New type parameter
        self.__real_time_dl = preferences.real_time_dl
        self.__convert_to = getattr(preferences, 'convert_to', None)

        self.__c_quality = qualities[self.__quality_download]
        self.__fallback_ids = self.__ids

        self.__set_quality()
        if preferences.is_episode:
            self.__write_episode()
        else:
            self.__write_track()

    def __set_quality(self) -> None:
        self.__dw_quality = self.__c_quality['n_quality']
        self.__file_format = self.__c_quality['f_format']
        self.__song_quality = self.__c_quality['s_quality']

    def __set_song_path(self) -> None:
        # Retrieve custom formatting strings from preferences, if any.
        custom_dir_format = getattr(self.__preferences, 'custom_dir_format', None)
        custom_track_format = getattr(self.__preferences, 'custom_track_format', None)
        pad_tracks = getattr(self.__preferences, 'pad_tracks', True)
        self.__song_path = set_path(
            self.__song_metadata,
            self.__output_dir,
            self.__song_quality,
            self.__file_format,
            self.__method_save,
            custom_dir_format=custom_dir_format,
            custom_track_format=custom_track_format,
            pad_tracks=pad_tracks
        )

    def __set_episode_path(self) -> None:
        custom_dir_format = getattr(self.__preferences, 'custom_dir_format', None)
        custom_track_format = getattr(self.__preferences, 'custom_track_format', None)
        pad_tracks = getattr(self.__preferences, 'pad_tracks', True)
        self.__song_path = set_path(
            self.__song_metadata,
            self.__output_dir,
            self.__song_quality,
            self.__file_format,
            self.__method_save,
            is_episode=True,
            custom_dir_format=custom_dir_format,
            custom_track_format=custom_track_format,
            pad_tracks=pad_tracks
        )

    def __write_track(self) -> None:
        self.__set_song_path()
        self.__c_track = Track(
            self.__song_metadata, self.__song_path,
            self.__file_format, self.__song_quality,
            self.__link, self.__ids
        )
        self.__c_track.md5_image = self.__ids
        self.__c_track.set_fallback_ids(self.__fallback_ids)

    def __write_episode(self) -> None:
        self.__set_episode_path()
        self.__c_episode = Episode(
            self.__song_metadata, self.__song_path,
            self.__file_format, self.__song_quality,
            self.__link, self.__ids
        )
        self.__c_episode.md5_image = self.__ids
        self.__c_episode.set_fallback_ids(self.__fallback_ids)

    def __convert_audio(self) -> None:
        # First, handle Spotify's OGG to standard format conversion (always needed)
        temp_filename = self.__song_path.replace(".ogg", ".tmp")
        os_replace(self.__song_path, temp_filename)
        
        # Register the temporary file
        register_active_download(temp_filename)
        
        try:
            # Step 1: First convert the OGG file to standard format
            ffmpeg_cmd = f"ffmpeg -y -hide_banner -loglevel error -i \"{temp_filename}\" -c:a copy \"{self.__song_path}\""
            system(ffmpeg_cmd)
            
            # Register the new output file and unregister the temp file
            register_active_download(self.__song_path)
            
            # Remove the temporary file
            if os.path.exists(temp_filename):
                remove(temp_filename)
                unregister_active_download(temp_filename)
            
            # Step 2: Convert to requested format if specified
            if self.__convert_to:
                format_name, bitrate = parse_format_string(self.__convert_to)
                if format_name:
                    try:
                        # Convert to the requested format using our standardized converter
                        converted_path = convert_audio(
                            self.__song_path,
                            format_name,
                            bitrate,
                            register_active_download,
                            unregister_active_download
                        )
                        if converted_path != self.__song_path:
                            # Update the path to the converted file
                            self.__song_path = converted_path
                            self.__c_track.song_path = converted_path
                    except Exception as conv_error:
                        # Log conversion error but continue with original file
                        logger.error(f"Audio conversion error: {str(conv_error)}")
                
        except Exception as e:
            # In case of failure, try to restore the original file
            if os.path.exists(temp_filename) and not os.path.exists(self.__song_path):
                os_replace(temp_filename, self.__song_path)
            
            # Clean up temp files
            if os.path.exists(temp_filename):
                remove(temp_filename)
                unregister_active_download(temp_filename)
                
            # Re-throw the exception
            raise e

    def get_no_dw_track(self) -> Track:
        return self.__c_track

    def easy_dw(self) -> Track:
        # Request the image data
        pic = self.__song_metadata['image']
        image = request(pic).content
        self.__song_metadata['image'] = image

        try:
            # Initialize success to False, it will be set to True if download_try is successful
            if hasattr(self, '_EASY_DW__c_track') and self.__c_track:
                self.__c_track.success = False
            elif hasattr(self, '_EASY_DW__c_episode') and self.__c_episode: # For episodes
                self.__c_episode.success = False
            
            self.download_try() # This should set self.__c_track.success = True if successful

        except Exception as e:
            song_title = self.__song_metadata.get('music', 'Unknown Song')
            artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
            error_message = f"Download failed for '{song_title}' by '{artist_name}' (URL: {self.__link}). Original error: {str(e)}"
            logger.error(error_message)
            traceback.print_exc()
            # Store the error message on the track object if it exists
            if hasattr(self, '_EASY_DW__c_track') and self.__c_track:
                self.__c_track.success = False
                self.__c_track.error_message = error_message # Store the more detailed error message
            # Removed problematic elif for __c_episode here as easy_dw in spotloader is focused on tracks.
            # Episode-specific error handling should be within download_eps or its callers.
            raise TrackNotFound(message=error_message, url=self.__link) from e
        
        # If the track was skipped (e.g. file already exists), return it immediately.
        # download_try sets success=False and was_skipped=True in this case.
        if hasattr(self, '_EASY_DW__c_track') and self.__c_track and getattr(self.__c_track, 'was_skipped', False):
            return self.__c_track

        # Final check for non-skipped tracks that might have failed after download_try returned.
        # This handles cases where download_try didn't raise an exception but self.__c_track.success is still False.
        if hasattr(self, '_EASY_DW__c_track') and self.__c_track and not self.__c_track.success:
            song_title = self.__song_metadata.get('music', 'Unknown Song')
            artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
            original_error_msg = getattr(self.__c_track, 'error_message', "Download failed for an unspecified reason after attempt.")
            error_msg_template = "Cannot download '{title}' by '{artist}'. Reason: {reason}"
            final_error_msg = error_msg_template.format(title=song_title, artist=artist_name, reason=original_error_msg)
            current_link = self.__c_track.link if hasattr(self.__c_track, 'link') and self.__c_track.link else self.__link
            logger.error(f"{final_error_msg} (URL: {current_link})")
            self.__c_track.error_message = final_error_msg # Ensure the most specific error is on the object
            raise TrackNotFound(message=final_error_msg, url=current_link)
            
        # If we reach here, the track should be successful and not skipped.
        if hasattr(self, '_EASY_DW__c_track') and self.__c_track and self.__c_track.success:
            write_tags(self.__c_track)
        
        return self.__c_track # Return the track object

    def track_exists(self, title, album):
        try:
            # Ensure the final song path is set
            if not hasattr(self, '_EASY_DW__song_path') or not self.__song_path:
                self.__set_song_path()

            # Use only the final directory for scanning
            final_dir = os.path.dirname(self.__song_path)
            
            # If the final directory doesn't exist, there are no files to check
            if not os.path.exists(final_dir):
                return False

            # Iterate over files only in the final directory
            for file in os.listdir(final_dir):
                if file.lower().endswith(('.mp3', '.ogg', '.flac', '.wav', '.m4a', '.opus')):
                    file_path = os.path.join(final_dir, file)
                    existing_title, existing_album = self.read_metadata(file_path)
                    if existing_title == title and existing_album == album:
                        logger.info(f"Found existing track: {title} - {album}")
                        return True
            return False
        except Exception as e:
            logger.error(f"Error checking if track exists: {str(e)}")
            return False

    def read_metadata(self, file_path):
        try:
            if not os.path.isfile(file_path):
                return None, None
            audio = File(file_path)
            if audio is None:
                return None, None
            title = None
            album = None
            if file_path.endswith('.mp3'):
                try:
                    audio = EasyID3(file_path)
                    title = audio.get('title', [None])[0]
                    album = audio.get('album', [None])[0]
                except Exception as e:
                    logger.error(f"Error reading MP3 metadata: {str(e)}")
            elif file_path.endswith('.ogg'):
                audio = OggVorbis(file_path)
                title = audio.get('title', [None])[0]
                album = audio.get('album', [None])[0]
            elif file_path.endswith('.flac'):
                audio = FLAC(file_path)
                title = audio.get('title', [None])[0]
                album = audio.get('album', [None])[0]
            elif file_path.endswith('.m4a'):
                audio = MP4(file_path)
                title = audio.get('\xa9nam', [None])[0]
                album = audio.get('\xa9alb', [None])[0]
            else:
                return None, None
            return title, album
        except Exception as e:
            logger.error(f"Error reading metadata from {file_path}: {str(e)}")
            return None, None

    def download_try(self) -> Track:
        current_title = self.__song_metadata.get('music')
        current_album = self.__song_metadata.get('album')
        current_artist = self.__song_metadata.get('artist')

        if self.track_exists(current_title, current_album):
            # Create skipped progress report using new format
            progress_data = {
                "type": "track",
                "song": current_title,
                "artist": current_artist,
                "status": "skipped",
                "url": self.__link,
                "reason": "Track already exists",
                "convert_to": self.__convert_to
            }
            
            # Add parent info based on parent type
            if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                playlist_data = self.__preferences.json_data
                playlist_name = playlist_data.get('name', 'unknown')
                total_tracks = playlist_data.get('tracks', {}).get('total', 'unknown')
                current_track = getattr(self.__preferences, 'track_number', 0)
                
                progress_data.update({
                    "current_track": current_track,
                    "total_tracks": total_tracks,
                    "parent": {
                        "type": "playlist",
                        "name": playlist_name,
                        "owner": playlist_data.get('owner', {}).get('display_name', 'unknown')
                    }
                })
            elif self.__parent == "album":
                album_name = self.__song_metadata.get('album', '')
                album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('ar_album', ''))
                total_tracks = self.__song_metadata.get('nb_tracks', 0)
                current_track = getattr(self.__preferences, 'track_number', 0)
                
                progress_data.update({
                    "current_track": current_track,
                    "total_tracks": total_tracks,
                    "parent": {
                        "type": "album",
                        "title": album_name,
                        "artist": album_artist
                    }
                })
                
            Download_JOB.report_progress(progress_data)
            
            # Mark track as intentionally skipped
            self.__c_track.success = False
            self.__c_track.was_skipped = True
            return self.__c_track

        retries = 0
        # Use the customizable retry parameters
        retry_delay = getattr(self.__preferences, 'initial_retry_delay', 30)  # Default to 30 seconds
        retry_delay_increase = getattr(self.__preferences, 'retry_delay_increase', 30)  # Default to 30 seconds
        max_retries = getattr(self.__preferences, 'max_retries', 5)  # Default to 5 retries

        while True:
            try:
                track_id_obj = TrackId.from_base62(self.__ids)
                stream = Download_JOB.session.content_feeder().load_track(
                    track_id_obj,
                    VorbisOnlyAudioQuality(self.__dw_quality),
                    False,
                    None
                )
                c_stream = stream.input_stream.stream()
                total_size = stream.input_stream.size
                
                os.makedirs(dirname(self.__song_path), exist_ok=True)
                
                # Register this file as being actively downloaded
                register_active_download(self.__song_path)
                
                try:
                    with open(self.__song_path, "wb") as f:
                        if self.__real_time_dl and self.__song_metadata.get("duration"):
                            # Real-time download path
                            duration = self.__song_metadata["duration"]
                            if duration > 0:
                                rate_limit = total_size / duration
                                chunk_size = 4096
                                bytes_written = 0
                                start_time = time.time()
                                
                                # Initialize tracking variable for percentage reporting
                                self._last_reported_percentage = -1
                                
                                while True:
                                    chunk = c_stream.read(chunk_size)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                                    bytes_written += len(chunk)
                                    
                                    # Calculate current percentage (as integer)
                                    current_time = time.time()
                                    current_percentage = int((bytes_written / total_size) * 100)
                                    
                                    # Only report when percentage increases by at least 1 point
                                    if current_percentage > self._last_reported_percentage:
                                        self._last_reported_percentage = current_percentage
                                        
                                        # Create real-time progress data
                                        progress_data = {
                                            "type": "track",
                                            "song": self.__song_metadata.get("music", ""),
                                            "artist": self.__song_metadata.get("artist", ""),
                                            "status": "real-time",
                                            "url": self.__link,
                                            "time_elapsed": int((current_time - start_time) * 1000),
                                            "progress": current_percentage,
                                            "convert_to": self.__convert_to
                                        }
                                        
                                        # Add parent info based on parent type
                                        if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                                            playlist_data = self.__preferences.json_data
                                            playlist_name = playlist_data.get('name', 'unknown')
                                            total_tracks = playlist_data.get('tracks', {}).get('total', 'unknown')
                                            current_track = getattr(self.__preferences, 'track_number', 0)
                                            playlist_owner = playlist_data.get('owner', {}).get('display_name', 'unknown')
                                            playlist_id = playlist_data.get('id', '')
                                            
                                            progress_data.update({
                                                "current_track": current_track,
                                                "total_tracks": total_tracks,
                                                "parent": {
                                                    "type": "playlist",
                                                    "name": playlist_name,
                                                    "owner": playlist_owner,
                                                    "total_tracks": total_tracks,
                                                    "url": f"https://open.spotify.com/playlist/{playlist_id}"
                                                }
                                            })
                                        elif self.__parent == "album":
                                            album_name = self.__song_metadata.get('album', '')
                                            album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('ar_album', ''))
                                            total_tracks = self.__song_metadata.get('nb_tracks', 0)
                                            current_track = getattr(self.__preferences, 'track_number', 0)
                                            
                                            progress_data.update({
                                                "current_track": current_track,
                                                "total_tracks": total_tracks,
                                                "parent": {
                                                    "type": "album",
                                                    "title": album_name,
                                                    "artist": album_artist,
                                                    "total_tracks": total_tracks,
                                                    "url": f"https://open.spotify.com/album/{self.__song_metadata.get('album_id', '')}"
                                                }
                                            })
                                        
                                        # Report the progress
                                        Download_JOB.report_progress(progress_data)
                                        
                                    # Rate limiting (if needed)
                                    expected_time = bytes_written / rate_limit
                                    if expected_time > (time.time() - start_time):
                                        time.sleep(expected_time - (time.time() - start_time))
                        else:
                            # Non real-time download path
                            data = c_stream.read(total_size)
                            f.write(data)
                    
                    # Close the stream after successful write
                    c_stream.close()
                    
                    # After successful download, unregister the file
                    unregister_active_download(self.__song_path)
                    break
                    
                except Exception as e:
                    # Handle any exceptions that might occur during download
                    error_msg = f"Error during download process: {str(e)}"
                    logger.error(error_msg)
                    
                    # Clean up resources
                    if 'c_stream' in locals():
                        try:
                            c_stream.close()
                        except Exception:
                            pass
                    
                    # Remove partial download if it exists
                    if os.path.exists(self.__song_path):
                        try:
                            os.remove(self.__song_path)
                        except Exception:
                            pass
                    
                    # Unregister the download
                    unregister_active_download(self.__song_path)
                    
                # After successful download, unregister the file (moved here from below)
                unregister_active_download(self.__song_path)
                break
                
            except Exception as e:
                # Handle retry logic
                global GLOBAL_RETRY_COUNT
                GLOBAL_RETRY_COUNT += 1
                retries += 1
                
                # Clean up any incomplete file
                if os.path.exists(self.__song_path):
                    os.remove(self.__song_path)
                unregister_active_download(self.__song_path)
                progress_data = {
                    "type": "track",
                    "status": "retrying",
                    "retry_count": retries,
                    "seconds_left": retry_delay,
                    "song": self.__song_metadata.get('music', ''),
                    "artist": self.__song_metadata.get('artist', ''),
                    "album": self.__song_metadata.get('album', ''),
                    "error": str(e),
                    "url": self.__link,
                    "convert_to": self.__convert_to
                }
                
                # Add parent info based on parent type
                if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                    playlist_data = self.__preferences.json_data
                    playlist_name = playlist_data.get('name', 'unknown')
                    total_tracks = playlist_data.get('tracks', {}).get('total', 'unknown')
                    current_track = getattr(self.__preferences, 'track_number', 0)
                    playlist_owner = playlist_data.get('owner', {}).get('display_name', 'unknown')
                    playlist_id = playlist_data.get('id', '')
                    
                    progress_data.update({
                        "current_track": current_track,
                        "total_tracks": total_tracks,
                        "parent": {
                            "type": "playlist",
                            "name": playlist_name,
                            "owner": playlist_owner,
                            "total_tracks": total_tracks,
                            "url": f"https://open.spotify.com/playlist/{playlist_id}"
                        }
                    })
                elif self.__parent == "album":
                    album_name = self.__song_metadata.get('album', '')
                    album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('ar_album', ''))
                    total_tracks = self.__song_metadata.get('nb_tracks', 0)
                    current_track = getattr(self.__preferences, 'track_number', 0)
                    album_id = self.__song_metadata.get('album_id', '')
                    
                    progress_data.update({
                        "current_track": current_track,
                        "total_tracks": total_tracks,
                        "parent": {
                            "type": "album",
                            "title": album_name,
                            "artist": album_artist,
                            "total_tracks": total_tracks,
                            "url": f"https://open.spotify.com/album/{album_id}"
                        }
                    })
                    
                Download_JOB.report_progress(progress_data)
                if retries >= max_retries or GLOBAL_RETRY_COUNT >= GLOBAL_MAX_RETRIES:
                    # Final cleanup before giving up
                    if os.path.exists(self.__song_path):
                        os.remove(self.__song_path)
                    # Add track info to exception    
                    track_name = self.__song_metadata.get('music', 'Unknown Track')
                    artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
                    final_error_msg = f"Maximum retry limit reached for '{track_name}' by '{artist_name}' (local: {max_retries}, global: {GLOBAL_MAX_RETRIES}). Last error: {str(e)}"
                    # Store error on track object
                    if hasattr(self, '_EASY_DW__c_track') and self.__c_track:
                        self.__c_track.success = False
                        self.__c_track.error_message = final_error_msg
                    raise Exception(final_error_msg) from e
                time.sleep(retry_delay)
                retry_delay += retry_delay_increase  # Use the custom retry delay increase
                
        try:
            self.__convert_audio()
        except Exception as e:
            # Improve error message formatting
            original_error_str = str(e)
            if "codec" in original_error_str.lower():
                error_msg = "Audio conversion error - Missing codec or unsupported format"
            elif "ffmpeg" in original_error_str.lower():
                error_msg = "FFmpeg error - Audio conversion failed"
            else:
                error_msg = f"Audio conversion failed: {original_error_str}"
            
            # Create standardized error format
            progress_data = {
                "type": "track",
                "status": "error",
                "song": self.__song_metadata.get('music', ''),
                "artist": self.__song_metadata.get('artist', ''),
                "error": error_msg,
                "url": self.__link,
                "convert_to": self.__convert_to
            }
            
            # Add parent info based on parent type
            if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                playlist_data = self.__preferences.json_data
                playlist_name = playlist_data.get('name', 'unknown')
                total_tracks = playlist_data.get('tracks', {}).get('total', 'unknown')
                current_track = getattr(self.__preferences, 'track_number', 0)
                playlist_owner = playlist_data.get('owner', {}).get('display_name', 'unknown')
                playlist_id = playlist_data.get('id', '')
                
                progress_data.update({
                    "current_track": current_track,
                    "total_tracks": total_tracks,
                    "parent": {
                        "type": "playlist",
                        "name": playlist_name,
                        "owner": playlist_owner,
                        "total_tracks": total_tracks,
                        "url": f"https://open.spotify.com/playlist/{playlist_id}"
                    }
                })
            elif self.__parent == "album":
                album_name = self.__song_metadata.get('album', '')
                album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('ar_album', ''))
                total_tracks = self.__song_metadata.get('nb_tracks', 0)
                current_track = getattr(self.__preferences, 'track_number', 0)
                album_id = self.__song_metadata.get('album_id', '')
                
                progress_data.update({
                    "current_track": current_track,
                    "total_tracks": total_tracks,
                    "parent": {
                        "type": "album",
                        "title": album_name,
                        "artist": album_artist,
                        "total_tracks": total_tracks,
                        "url": f"https://open.spotify.com/album/{album_id}"
                    }
                })
            
            # Report the error
            Download_JOB.report_progress(progress_data)
            logger.error(f"Audio conversion error: {error_msg}")
            
            # If conversion fails, clean up the .ogg file
            if os.path.exists(self.__song_path):
                os.remove(self.__song_path)
                
            # Try one more time
            time.sleep(retry_delay)
            retry_delay += retry_delay_increase
            try:
                self.__convert_audio()
            except Exception as conv_e:
                # If conversion fails twice, create a final error report
                error_msg = f"Audio conversion failed after retry for '{self.__song_metadata.get('music', 'Unknown Track')}'. Original error: {str(conv_e)}"
                progress_data["error"] = error_msg
                progress_data["status"] = "error"
                Download_JOB.report_progress(progress_data)
                logger.error(error_msg)
                
                if os.path.exists(self.__song_path):
                    os.remove(self.__song_path)
                # Store error on track object
                if hasattr(self, '_EASY_DW__c_track') and self.__c_track:
                    self.__c_track.success = False
                    self.__c_track.error_message = error_msg
                raise TrackNotFound(message=error_msg, url=self.__link) from conv_e

        if hasattr(self, '_EASY_DW__c_track') and self.__c_track: 
            self.__c_track.success = True
            self.__write_track()
            write_tags(self.__c_track)
        
        # Create done status report using the same format as progress status
        progress_data = {
            "type": "track",
            "song": self.__song_metadata.get("music", ""),
            "artist": self.__song_metadata.get("artist", ""),
            "status": "done",
            "url": self.__link,
            "convert_to": self.__convert_to
        }
        
        # Add parent info based on parent type
        if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
            playlist_data = self.__preferences.json_data
            playlist_name = playlist_data.get('name', 'unknown')
            total_tracks = playlist_data.get('tracks', {}).get('total', 'unknown')
            current_track = getattr(self.__preferences, 'track_number', 0)
            
            progress_data.update({
                "current_track": current_track,
                "total_tracks": total_tracks,
                "parent": {
                    "type": "playlist",
                    "name": playlist_name,
                    "owner": playlist_data.get('owner', {}).get('display_name', 'unknown'),
                    "total_tracks": total_tracks,
                    "url": f"https://open.spotify.com/playlist/{playlist_data.get('id', '')}"
                }
            })
        elif self.__parent == "album":
            album_name = self.__song_metadata.get('album', '')
            album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('ar_album', ''))
            total_tracks = self.__song_metadata.get('nb_tracks', 0)
            current_track = getattr(self.__preferences, 'track_number', 0)
            
            progress_data.update({
                "current_track": current_track,
                "total_tracks": total_tracks,
                "parent": {
                    "type": "album",
                    "title": album_name,
                    "artist": album_artist,
                    "total_tracks": total_tracks,
                    "url": f"https://open.spotify.com/album/{self.__song_metadata.get('album_id', '')}"
                }
            })
            
        Download_JOB.report_progress(progress_data)
        return self.__c_track

    def download_eps(self) -> Episode:
        # Use the customizable retry parameters
        retry_delay = getattr(self.__preferences, 'initial_retry_delay', 30)  # Default to 30 seconds
        retry_delay_increase = getattr(self.__preferences, 'retry_delay_increase', 30)  # Default to 30 seconds
        max_retries = getattr(self.__preferences, 'max_retries', 5)  # Default to 5 retries
        
        retries = 0
        if isfile(self.__song_path) and check_track(self.__c_episode):
            ans = input(
                f"Episode \"{self.__song_path}\" already exists, do you want to redownload it?(y or n):"
            )
            if not ans in answers:
                return self.__c_episode
        episode_id = EpisodeId.from_base62(self.__ids)
        while True:
            try:
                stream = Download_JOB.session.content_feeder().load_episode(
                    episode_id,
                    AudioQuality(self.__dw_quality),
                    False,
                    None
                )
                break
            except Exception as e:
                global GLOBAL_RETRY_COUNT
                GLOBAL_RETRY_COUNT += 1
                retries += 1
                print(json.dumps({
                    "status": "retrying",
                    "retry_count": retries,
                    "seconds_left": retry_delay,
                    "song": self.__song_metadata['music'],
                    "artist": self.__song_metadata['artist'],
                    "album": self.__song_metadata['album'],
                    "error": str(e),
                    "convert_to": self.__convert_to
                }))
                if retries >= max_retries or GLOBAL_RETRY_COUNT >= GLOBAL_MAX_RETRIES:
                    # Clean up any partial files before giving up
                    if os.path.exists(self.__song_path):
                        os.remove(self.__song_path)
                    # Add track info to exception    
                    track_name = self.__song_metadata.get('music', 'Unknown Track')
                    artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
                    final_error_msg = f"Maximum retry limit reached for '{track_name}' by '{artist_name}' (local: {max_retries}, global: {GLOBAL_MAX_RETRIES}). Last error: {str(e)}"
                    # Store error on track object
                    if hasattr(self, '_EASY_DW__c_episode') and self.__c_episode:
                        self.__c_episode.success = False
                        self.__c_episode.error_message = final_error_msg
                    raise Exception(final_error_msg) from e
                time.sleep(retry_delay)
                retry_delay += retry_delay_increase  # Use the custom retry delay increase
        total_size = stream.input_stream.size
        os.makedirs(dirname(self.__song_path), exist_ok=True)
        
        # Register this file as being actively downloaded
        register_active_download(self.__song_path)
        
        try:
            with open(self.__song_path, "wb") as f:
                c_stream = stream.input_stream.stream()
                if self.__real_time_dl and self.__song_metadata.get("duration"):
                    duration = self.__song_metadata["duration"]
                    if duration > 0:
                        rate_limit = total_size / duration
                        chunk_size = 4096
                        bytes_written = 0
                        start_time = time.time()
                        try:
                            while True:
                                chunk = c_stream.read(chunk_size)
                                if not chunk:
                                    break
                                f.write(chunk)
                                bytes_written += len(chunk)
                                # Could add progress reporting here
                                expected_time = bytes_written / rate_limit
                                elapsed_time = time.time() - start_time
                                if expected_time > elapsed_time:
                                    time.sleep(expected_time - elapsed_time)
                        except Exception as e:
                            # If any error occurs during real-time download, delete the incomplete file
                            logger.error(f"Error during real-time download: {str(e)}")
                            try:
                                c_stream.close()
                            except:
                                pass
                            try:
                                f.close()
                            except:
                                pass
                            if os.path.exists(self.__song_path):
                                os.remove(self.__song_path)
                            # Add track info to exception    
                            track_name = self.__song_metadata.get('music', 'Unknown Track')
                            artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
                            final_error_msg = f"Error during real-time download for '{track_name}' by '{artist_name}' (URL: {self.__link}). Error: {str(e)}"
                            # Store error on track object
                            if hasattr(self, '_EASY_DW__c_episode') and self.__c_episode:
                                self.__c_episode.success = False
                                self.__c_episode.error_message = final_error_msg
                            raise TrackNotFound(message=final_error_msg, url=self.__link) from e
                    else:
                        try:
                            data = c_stream.read(total_size)
                            f.write(data)
                        except Exception as e:
                            logger.error(f"Error during episode download: {str(e)}")
                            try:
                                c_stream.close()
                            except:
                                pass
                            if os.path.exists(self.__song_path):
                                os.remove(self.__song_path)
                            # Add track info to exception    
                            track_name = self.__song_metadata.get('music', 'Unknown Track')
                            artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
                            final_error_msg = f"Error during episode download for '{track_name}' by '{artist_name}' (URL: {self.__link}). Error: {str(e)}"
                            # Store error on track object
                            if hasattr(self, '_EASY_DW__c_episode') and self.__c_episode:
                                self.__c_episode.success = False
                                self.__c_episode.error_message = final_error_msg
                            raise TrackNotFound(message=final_error_msg, url=self.__link) from e
                else:
                    try:
                        data = c_stream.read(total_size)
                        f.write(data)
                    except Exception as e:
                        logger.error(f"Error during episode download: {str(e)}")
                        try:
                            c_stream.close()
                        except:
                            pass
                        if os.path.exists(self.__song_path):
                            os.remove(self.__song_path)
                        # Add track info to exception    
                        track_name = self.__song_metadata.get('music', 'Unknown Track')
                        artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
                        final_error_msg = f"Error during episode download for '{track_name}' by '{artist_name}' (URL: {self.__link}). Error: {str(e)}"
                        # Store error on track object
                        if hasattr(self, '_EASY_DW__c_episode') and self.__c_episode:
                            self.__c_episode.success = False
                            self.__c_episode.error_message = final_error_msg
                        raise TrackNotFound(message=final_error_msg, url=self.__link) from e
                c_stream.close()
        except Exception as e:
            # Clean up the file on any error
            if os.path.exists(self.__song_path):
                os.remove(self.__song_path)
            unregister_active_download(self.__song_path)
            episode_title = self.__song_metadata.get('music', 'Unknown Episode')
            error_message = f"Failed to download episode '{episode_title}' (URL: {self.__link}). Error: {str(e)}"
            logger.error(error_message)
            # Store error on episode object
            if hasattr(self, '_EASY_DW__c_episode') and self.__c_episode:
                self.__c_episode.success = False
                self.__c_episode.error_message = error_message
            raise TrackNotFound(message=error_message, url=self.__link) from e
            
        try:
            self.__convert_audio()
        except Exception as e:
            logger.error(json.dumps({
                "status": "retrying",
                "action": "convert_audio",
                "song": self.__song_metadata['music'],
                "artist": self.__song_metadata['artist'],
                "album": self.__song_metadata['album'],
                "error": str(e),
                "convert_to": self.__convert_to
            }))
            # Clean up if conversion fails
            if os.path.exists(self.__song_path):
                os.remove(self.__song_path)
                
            time.sleep(retry_delay)
            retry_delay += retry_delay_increase  # Use the custom retry delay increase
            try:
                self.__convert_audio()
            except Exception as conv_e:
                # If conversion fails twice, clean up and raise
                if os.path.exists(self.__song_path):
                    os.remove(self.__song_path)
                episode_title = self.__song_metadata.get('music', 'Unknown Episode')
                error_message = f"Audio conversion for episode '{episode_title}' failed after retry. Original error: {str(conv_e)}"
                logger.error(error_message)
                # Store error on episode object
                self.__c_episode.success = False
                self.__c_episode.error_message = error_message
                raise TrackNotFound(message=error_message, url=self.__link) from conv_e
                
        self.__write_episode()
        # Write metadata tags so subsequent skips work
        write_tags(self.__c_episode)
        return self.__c_episode

def download_cli(preferences: Preferences) -> None:
    __link = preferences.link
    __output_dir = preferences.output_dir
    __method_save = preferences.method_save
    __not_interface = preferences.not_interface
    __quality_download = preferences.quality_download
    __recursive_download = preferences.recursive_download
    __recursive_quality = preferences.recursive_quality
    cmd = f"deez-dw.py -so spo -l \"{__link}\" "
    if __output_dir:
        cmd += f"-o {__output_dir} "
    if __method_save:
        cmd += f"-sa {__method_save} "
    if __not_interface:
        cmd += f"-g "
    if __quality_download:
        cmd += f"-q {__quality_download} "
    if __recursive_download:
        cmd += f"-rd "
    if __recursive_quality:
        cmd += f"-rq"
    system(cmd)

class DW_TRACK:
    def __init__(
        self,
        preferences: Preferences
    ) -> None:
        self.__preferences = preferences

    def dw(self) -> Track:
        track = EASY_DW(self.__preferences).easy_dw()
        # No error handling needed here - if track.success is False but was_skipped is True,
        # it's an intentional skip, not an error
        return track

    def dw2(self) -> Track:
        track = EASY_DW(self.__preferences).get_no_dw_track()
        download_cli(self.__preferences)
        return track

class DW_ALBUM:
    def __init__(
        self,
        preferences: Preferences
    ) -> None:
        self.__preferences = preferences
        self.__ids = self.__preferences.ids
        self.__make_zip = self.__preferences.make_zip
        self.__output_dir = self.__preferences.output_dir
        self.__method_save = self.__preferences.method_save
        self.__song_metadata = self.__preferences.song_metadata
        self.__not_interface = self.__preferences.not_interface
        self.__song_metadata_items = self.__song_metadata.items()

    def dw(self) -> Album:
        # Helper function to find most frequent item in a list
        def most_frequent(items):
            if not items:
                return None
            # If items is a string with semicolons, split it
            if isinstance(items, str) and ";" in items:
                items = [item.strip() for item in items.split(";")]
            # If it's still a string, return it directly
            if isinstance(items, str):
                return items
            # Otherwise, find the most frequent item
            return max(set(items), key=items.count)
        
        # Report album initializing status
        album_name = self.__song_metadata.get('album', 'Unknown Album')
        
        # Process album artist to get the most representative one
        album_artist = self.__song_metadata.get('artist', 'Unknown Artist')
        if isinstance(album_artist, list):
            album_artist = most_frequent(album_artist)
        elif isinstance(album_artist, str) and ";" in album_artist:
            artists_list = [artist.strip() for artist in album_artist.split(";")]
            album_artist = most_frequent(artists_list) if artists_list else album_artist
        
        total_tracks = self.__song_metadata.get('nb_tracks', 0)
        album_id = self.__ids
        
        Download_JOB.report_progress({
            "type": "album",
            "artist": album_artist,
            "status": "initializing",
            "total_tracks": total_tracks,
            "title": album_name,
            "url": f"https://open.spotify.com/album/{album_id}"
        })
        
        pic = self.__song_metadata['image']
        image = request(pic).content
        self.__song_metadata['image'] = image
        album = Album(self.__ids)
        album.image = image
        album.nb_tracks = self.__song_metadata['nb_tracks']
        album.album_name = self.__song_metadata['album']
        album.upc = self.__song_metadata['upc']
        tracks = album.tracks
        album.md5_image = self.__ids
        album.tags = self.__song_metadata
        
        c_song_metadata = {}
        for key, item in self.__song_metadata_items:
            if type(item) is not list:
                c_song_metadata[key] = self.__song_metadata[key]
        total_tracks = album.nb_tracks
        for a in range(total_tracks):
            for key, item in self.__song_metadata_items:
                if type(item) is list:
                    c_song_metadata[key] = self.__song_metadata[key][a]
            song_name = c_song_metadata['music']
            artist_name = c_song_metadata['artist']
            album_name = c_song_metadata['album']
            current_track = a + 1
            
            c_preferences = deepcopy(self.__preferences)
            c_preferences.song_metadata = c_song_metadata.copy()
            c_preferences.ids = c_song_metadata['ids']
            c_preferences.track_number = current_track  # Track number in the album
            c_preferences.link = f"https://open.spotify.com/track/{c_preferences.ids}"
            
            # Add album_id to song metadata for consistent parent info
            c_preferences.song_metadata['album_id'] = self.__ids
            
            try:
                # Use track-level reporting through EASY_DW
                track = EASY_DW(c_preferences, parent='album').download_try()
            except TrackNotFound as e_track:
                track = Track(c_song_metadata, None, None, None, None, None)
                track.success = False
                track.error_message = str(e_track) # Store the error message from TrackNotFound
                logger.warning(f"Track '{song_name}' by '{artist_name}' from album '{album.album_name}' not found or failed to download. Reason: {track.error_message}")
            except Exception as e_generic:
                track = Track(c_song_metadata, None, None, None, None, None)
                track.success = False
                track.error_message = f"An unexpected error occurred: {str(e_generic)}"
                logger.error(f"Unexpected error downloading track '{song_name}' by '{artist_name}' from album '{album.album_name}'. Reason: {track.error_message}")
            tracks.append(track)
        if self.__make_zip:
            song_quality = tracks[0].quality
            custom_dir_format = getattr(self.__preferences, 'custom_dir_format', None)
            zip_name = create_zip(
                tracks,
                output_dir=self.__output_dir,
                song_metadata=self.__song_metadata,
                song_quality=song_quality,
                method_save=self.__method_save,
                custom_dir_format=custom_dir_format
            )
            album.zip_path = zip_name
            
        # Report album done status
        album_name = self.__song_metadata.get('album', 'Unknown Album')
        
        # Process album artist for the done status (use the same logic as initializing)
        album_artist = self.__song_metadata.get('artist', 'Unknown Artist')
        if isinstance(album_artist, list):
            album_artist = most_frequent(album_artist)
        elif isinstance(album_artist, str) and ";" in album_artist:
            artists_list = [artist.strip() for artist in album_artist.split(";")]
            album_artist = most_frequent(artists_list) if artists_list else album_artist
        
        total_tracks = self.__song_metadata.get('nb_tracks', 0)
        album_id = self.__ids
        
        Download_JOB.report_progress({
            "type": "album",
            "artist": album_artist,
            "status": "done",
            "total_tracks": total_tracks,
            "title": album_name,
            "url": f"https://open.spotify.com/album/{album_id}"
        })
        
        return album

    def dw2(self) -> Album:
        track = EASY_DW(self.__preferences).get_no_dw_track()
        download_cli(self.__preferences)
        return track

class DW_PLAYLIST:
    def __init__(
        self,
        preferences: Preferences
    ) -> None:
        self.__preferences = preferences
        self.__ids = self.__preferences.ids
        self.__json_data = preferences.json_data
        self.__make_zip = self.__preferences.make_zip
        self.__output_dir = self.__preferences.output_dir
        self.__song_metadata = self.__preferences.song_metadata

    def dw(self) -> Playlist:
        playlist_name = self.__json_data.get('name', 'unknown')
        total_tracks = self.__json_data.get('tracks', {}).get('total', 'unknown')
        playlist_owner = self.__json_data.get('owner', {}).get('display_name', 'Unknown Owner')
        playlist_id = self.__ids

        # Report playlist initializing status
        Download_JOB.report_progress({
            "type": "playlist",
            "owner": playlist_owner,
            "status": "initializing",
            "total_tracks": total_tracks,
            "name": playlist_name,
            "url": f"https://open.spotify.com/playlist/{playlist_id}"
        })
        
        # --- Prepare the m3u playlist file ---
        playlist_m3u_dir = os.path.join(self.__output_dir, "playlists")
        os.makedirs(playlist_m3u_dir, exist_ok=True)
        m3u_path = os.path.join(playlist_m3u_dir, f"{playlist_name}.m3u")
        if not os.path.exists(m3u_path):
            with open(m3u_path, "w", encoding="utf-8") as m3u_file:
                m3u_file.write("#EXTM3U\n")
        # -------------------------------------

        playlist = Playlist()
        tracks = playlist.tracks
        for idx, c_song_metadata in enumerate(self.__song_metadata):
            if type(c_song_metadata) is str:
                print(f"Track not found {c_song_metadata} :(")
                continue
            c_preferences = deepcopy(self.__preferences)
            c_preferences.ids = c_song_metadata['ids']
            c_preferences.song_metadata = c_song_metadata
            c_preferences.json_data = self.__json_data  # Pass playlist data for reporting
            c_preferences.track_number = idx + 1  # Track number in the playlist

            # Use track-level reporting through EASY_DW
            track = EASY_DW(c_preferences, parent='playlist').easy_dw()

            # Only log a warning if the track failed and was NOT intentionally skipped
            if not track.success and not getattr(track, 'was_skipped', False):
                song = f"{c_song_metadata['music']} - {c_song_metadata['artist']}"
                error_detail = getattr(track, 'error_message', 'Download failed for unspecified reason.')
                logger.warning(f"Cannot download '{song}' from playlist '{playlist_name}'. Reason: {error_detail} (URL: {track.link or c_preferences.link})")

            tracks.append(track)
            # --- Append the final track path to the m3u file using a relative path ---
            if track.success and hasattr(track, 'song_path') and track.song_path:
                # Build the relative path from the playlists directory
                relative_path = os.path.relpath(
                    track.song_path,
                    start=os.path.join(self.__output_dir, "playlists")
                )
                with open(m3u_path, "a", encoding="utf-8") as m3u_file:
                    m3u_file.write(f"{relative_path}\n")
            # ---------------------------------------------------------------------
        
        if self.__make_zip:
            playlist_title = self.__json_data['name']
            zip_name = f"{self.__output_dir}/{playlist_title} [playlist {self.__ids}]"
            create_zip(tracks, zip_name=zip_name)
            playlist.zip_path = zip_name
            
        # Report playlist done status
        playlist_name = self.__json_data.get('name', 'Unknown Playlist')
        playlist_owner = self.__json_data.get('owner', {}).get('display_name', 'Unknown Owner')
        total_tracks = self.__json_data.get('tracks', {}).get('total', 0)
        playlist_id = self.__ids
        
        Download_JOB.report_progress({
            "type": "playlist",
            "owner": playlist_owner,
            "status": "done",
            "total_tracks": total_tracks,
            "name": playlist_name,
            "url": f"https://open.spotify.com/playlist/{playlist_id}"
        })
        
        return playlist

    def dw2(self) -> Playlist:
        # Extract playlist metadata for reporting
        playlist_name = self.__json_data.get('name', 'Unknown Playlist')
        playlist_owner = self.__json_data.get('owner', {}).get('display_name', 'Unknown Owner')
        total_tracks = self.__json_data.get('tracks', {}).get('total', 'unknown')
        playlist_id = self.__ids
        
        # Report playlist initializing status
        Download_JOB.report_progress({
            "type": "playlist",
            "owner": playlist_owner,
            "status": "initializing",
            "total_tracks": total_tracks,
            "name": playlist_name,
            "url": f"https://open.spotify.com/playlist/{playlist_id}"
        })
        
        playlist = Playlist()
        tracks = playlist.tracks
        for i, c_song_metadata in enumerate(self.__song_metadata):
            if type(c_song_metadata) is str:
                logger.warning(f"Track not found {c_song_metadata}")
                continue
            c_preferences = deepcopy(self.__preferences)
            c_preferences.ids = c_song_metadata['ids']
            c_preferences.song_metadata = c_song_metadata
            c_preferences.json_data = self.__json_data  # Pass playlist data for reporting
            c_preferences.track_number = i + 1  # Track number in the playlist

            # Even though we're not downloading directly, we still need to set up the track object
            track = EASY_DW(c_preferences, parent='playlist').get_no_dw_track()
            if not track.success:
                song = f"{c_song_metadata['music']} - {c_song_metadata['artist']}"
                error_detail = getattr(track, 'error_message', 'Download failed for unspecified reason.')
                logger.warning(f"Cannot download '{song}' (CLI mode). Reason: {error_detail} (Link: {track.link or c_preferences.link})")
            tracks.append(track)
            
            # Track-level progress reporting using the standardized format
            progress_data = {
                "type": "track",
                "song": c_song_metadata.get("music", ""),
                "artist": c_song_metadata.get("artist", ""),
                "status": "progress",
                "current_track": i + 1,
                "total_tracks": total_tracks,
                "parent": {
                    "type": "playlist",
                    "name": playlist_name,
                    "owner": self.__json_data.get('owner', {}).get('display_name', 'unknown'),
                    "total_tracks": total_tracks,
                    "url": f"https://open.spotify.com/playlist/{self.__json_data.get('id', '')}"
                },
                "url": f"https://open.spotify.com/track/{c_song_metadata['ids']}"
            }
            Download_JOB.report_progress(progress_data)
        download_cli(self.__preferences)
        
        if self.__make_zip:
            playlist_title = self.__json_data['name']
            zip_name = f"{self.__output_dir}/{playlist_title} [playlist {self.__ids}]"
            create_zip(tracks, zip_name=zip_name)
            playlist.zip_path = zip_name
            
        # Report playlist done status
        playlist_name = self.__json_data.get('name', 'Unknown Playlist')
        playlist_owner = self.__json_data.get('owner', {}).get('display_name', 'Unknown Owner')
        total_tracks = self.__json_data.get('tracks', {}).get('total', 0)
        playlist_id = self.__ids
        
        Download_JOB.report_progress({
            "type": "playlist",
            "owner": playlist_owner,
            "status": "done",
            "total_tracks": total_tracks,
            "name": playlist_name,
            "url": f"https://open.spotify.com/playlist/{playlist_id}"
        })
        
        return playlist

class DW_EPISODE:
    def __init__(
        self,
        preferences: Preferences
    ) -> None:
        self.__preferences = preferences

    def dw(self) -> Episode:
        # Using standardized episode progress format
        progress_data = {
            "type": "episode",
            "song": self.__preferences.song_metadata.get('name', 'Unknown Episode'),
            "artist": self.__preferences.song_metadata.get('show', 'Unknown Show'),
            "status": "initializing"
        }
        
        # Set URL if available
        episode_id = self.__preferences.ids
        if episode_id:
            progress_data["url"] = f"https://open.spotify.com/episode/{episode_id}"
            
        Download_JOB.report_progress(progress_data)
        
        episode = EASY_DW(self.__preferences).download_eps()
        
        # Using standardized episode progress format
        progress_data = {
            "type": "episode",
            "song": self.__preferences.song_metadata.get('name', 'Unknown Episode'),
            "artist": self.__preferences.song_metadata.get('show', 'Unknown Show'),
            "status": "done"
        }
        
        # Set URL if available
        episode_id = self.__preferences.ids
        if episode_id:
            progress_data["url"] = f"https://open.spotify.com/episode/{episode_id}"
            
        Download_JOB.report_progress(progress_data)
        
        return episode

    def dw2(self) -> Episode:
        # Using standardized episode progress format
        progress_data = {
            "type": "episode",
            "song": self.__preferences.song_metadata.get('name', 'Unknown Episode'),
            "artist": self.__preferences.song_metadata.get('show', 'Unknown Show'),
            "status": "initializing"
        }
        
        # Set URL if available
        episode_id = self.__preferences.ids
        if episode_id:
            progress_data["url"] = f"https://open.spotify.com/episode/{episode_id}"
            
        Download_JOB.report_progress(progress_data)
        
        episode = EASY_DW(self.__preferences).get_no_dw_track()
        download_cli(self.__preferences)
        
        # Using standardized episode progress format
        progress_data = {
            "type": "episode",
            "song": self.__preferences.song_metadata.get('name', 'Unknown Episode'),
            "artist": self.__preferences.song_metadata.get('show', 'Unknown Show'),
            "status": "done"
        }
        
        # Set URL if available
        episode_id = self.__preferences.ids
        if episode_id:
            progress_data["url"] = f"https://open.spotify.com/episode/{episode_id}"
            
        Download_JOB.report_progress(progress_data)
        
        return episode
