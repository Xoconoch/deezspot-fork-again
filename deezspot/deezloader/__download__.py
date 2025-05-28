#!/usr/bin/python3
import os
import json
import re
from os.path import isfile
from copy import deepcopy
from deezspot.libutils.audio_converter import convert_audio, parse_format_string
from deezspot.deezloader.dee_api import API
from deezspot.deezloader.deegw_api import API_GW
from deezspot.deezloader.deezer_settings import qualities
from deezspot.libutils.others_settings import answers
from deezspot.__taggers__ import write_tags, check_track
from deezspot.deezloader.__download_utils__ import decryptfile, gen_song_hash
from deezspot.exceptions import (
    TrackNotFound,
    NoRightOnMedia,
    QualityNotFound,
)
from deezspot.models import (
    Track,
    Album,
    Playlist,
    Preferences,
    Episode,
)
from deezspot.deezloader.__utils__ import (
    check_track_ids,
    check_track_token,
    check_track_md5,
)
from deezspot.libutils.utils import (
    set_path,
    trasform_sync_lyric,
    create_zip,
)
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen import File
from deezspot.libutils.logging_utils import logger, ProgressReporter

class Download_JOB:
    progress_reporter = None
    
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

    @classmethod
    def __get_url(cls, c_track: Track, quality_download: str) -> dict:
        if c_track.get('__TYPE__') == 'episode':
            return {
                "media": [{
                    "sources": [{
                        "url": c_track.get('EPISODE_DIRECT_STREAM_URL')
                    }]
                }]
            }
        else:
            # Get track IDs and check which encryption method is available
            track_info = check_track_ids(c_track)
            encryption_type = track_info.get('encryption_type', 'blowfish')
            
            # If AES encryption is available (MEDIA_KEY and MEDIA_NONCE present)
            if encryption_type == 'aes':
                # Use track token to get media URL from API
                track_token = check_track_token(c_track)
                medias = API_GW.get_medias_url([track_token], quality_download)
                return medias[0]
            
            # Use Blowfish encryption (legacy method)
            else:
                md5_origin = track_info.get('md5_origin')
                media_version = track_info.get('media_version', '1')
                track_id = track_info.get('track_id')
                
                if not md5_origin:
                    raise ValueError("MD5_ORIGIN is missing")
                if not track_id:
                    raise ValueError("Track ID is missing")
                
                n_quality = qualities[quality_download]['n_quality']
                
                # Create the song hash using the correct parameter order
                # Note: For legacy Deezer API, the order is: MD5 + Media Version + Track ID
                c_song_hash = gen_song_hash(track_id, md5_origin, media_version)
                
                # Log the hash generation parameters for debugging
                logger.debug(f"Generating song hash with: track_id={track_id}, md5_origin={md5_origin}, media_version={media_version}")
                
                c_media_url = API_GW.get_song_url(md5_origin[0], c_song_hash)
                
                return {
                    "media": [
                        {
                            "sources": [
                                {
                                    "url": c_media_url
                                }
                            ]
                        }
                    ]
                }
     
    @classmethod
    def check_sources(
        cls,
        infos_dw: list,
        quality_download: str  
    ) -> list:
        # Preprocess episodes separately
        medias = []
        for track in infos_dw:
            if track.get('__TYPE__') == 'episode':
                media_json = cls.__get_url(track, quality_download)
                medias.append(media_json)

        # For non-episodes, gather tokens
        non_episode_tracks = [c_track for c_track in infos_dw if c_track.get('__TYPE__') != 'episode']
        tokens = [check_track_token(c_track) for c_track in non_episode_tracks]

        def chunk_list(lst, chunk_size):
            """Yield successive chunk_size chunks from lst."""
            for i in range(0, len(lst), chunk_size):
                yield lst[i:i + chunk_size]

        # Prepare list for media results for non-episodes
        non_episode_medias = []

        # Split tokens into chunks of 25
        for tokens_chunk in chunk_list(tokens, 25):
            try:
                chunk_medias = API_GW.get_medias_url(tokens_chunk, quality_download)
                # Post-process each returned media in the chunk
                for idx in range(len(chunk_medias)):
                    if "errors" in chunk_medias[idx]:
                        c_media_json = cls.__get_url(non_episode_tracks[len(non_episode_medias) + idx], quality_download)
                        chunk_medias[idx] = c_media_json
                    else:
                        if not chunk_medias[idx]['media']:
                            c_media_json = cls.__get_url(non_episode_tracks[len(non_episode_medias) + idx], quality_download)
                            chunk_medias[idx] = c_media_json
                        elif len(chunk_medias[idx]['media'][0]['sources']) == 1:
                            c_media_json = cls.__get_url(non_episode_tracks[len(non_episode_medias) + idx], quality_download)
                            chunk_medias[idx] = c_media_json
                non_episode_medias.extend(chunk_medias)
            except NoRightOnMedia:
                for c_track in tokens_chunk:
                    # Find the corresponding full track info from non_episode_tracks
                    track_index = len(non_episode_medias)
                    c_media_json = cls.__get_url(non_episode_tracks[track_index], quality_download)
                    non_episode_medias.append(c_media_json)

        # Now, merge the medias. We need to preserve the original order.
        # We'll create a final list that contains media for each track in infos_dw.
        final_medias = []
        episode_idx = 0
        non_episode_idx = 0
        for track in infos_dw:
            if track.get('__TYPE__') == 'episode':
                final_medias.append(medias[episode_idx])
                episode_idx += 1
            else:
                final_medias.append(non_episode_medias[non_episode_idx])
                non_episode_idx += 1

        return final_medias

class EASY_DW:
    def __init__(
        self,
        infos_dw: dict,
        preferences: Preferences,
        parent: str = None  # Can be 'album', 'playlist', or None for individual track
    ) -> None:
        
        self.__preferences = preferences
        self.__parent = parent  # Store the parent type
        
        self.__infos_dw = infos_dw
        self.__ids = preferences.ids
        self.__link = preferences.link
        self.__output_dir = preferences.output_dir
        self.__method_save = preferences.method_save
        self.__not_interface = preferences.not_interface
        self.__quality_download = preferences.quality_download
        self.__recursive_quality = preferences.recursive_quality
        self.__recursive_download = preferences.recursive_download
        self.__convert_to = getattr(preferences, 'convert_to', None)


        if self.__infos_dw.get('__TYPE__') == 'episode':
            self.__song_metadata = {
                'music': self.__infos_dw.get('EPISODE_TITLE', ''),
                'artist': self.__infos_dw.get('SHOW_NAME', ''),
                'album': self.__infos_dw.get('SHOW_NAME', ''),
                'date': self.__infos_dw.get('EPISODE_PUBLISHED_TIMESTAMP', '').split()[0],
                'genre': 'Podcast',
                'explicit': self.__infos_dw.get('SHOW_IS_EXPLICIT', '2'),
                'disc': 1,
                'track': 1,
                'duration': int(self.__infos_dw.get('DURATION', 0)),
                'isrc': None
            }
            self.__download_type = "episode"
        else:
            self.__song_metadata = preferences.song_metadata
            self.__download_type = "track"

        self.__c_quality = qualities[self.__quality_download]
        self.__fallback_ids = self.__ids

        self.__set_quality()
        self.__write_track()

    def __track_already_exists(self, title, album):
        # Ensure the song path is set; if not, compute it.
        if not hasattr(self, '_EASY_DW__song_path') or not self.__song_path:
            self.__set_song_path()
        
        # Get only the final directory where the track will be saved.
        final_dir = os.path.dirname(self.__song_path)
        if not os.path.exists(final_dir):
            return False

        # List files only in the final directory.
        for file in os.listdir(final_dir):
            file_path = os.path.join(final_dir, file)
            lower_file = file.lower()
            try:
                existing_title = None
                existing_album = None
                if lower_file.endswith('.flac'):
                    audio = FLAC(file_path)
                    existing_title = audio.get('title', [None])[0]
                    existing_album = audio.get('album', [None])[0]
                elif lower_file.endswith('.mp3'):
                    audio = MP3(file_path, ID3=ID3)
                    existing_title = audio.get('TIT2', [None])[0]
                    existing_album = audio.get('TALB', [None])[0]
                elif lower_file.endswith('.m4a'):
                    audio = MP4(file_path)
                    existing_title = audio.get('\xa9nam', [None])[0]
                    existing_album = audio.get('\xa9alb', [None])[0]
                elif lower_file.endswith(('.ogg', '.wav')):
                    audio = File(file_path)
                    existing_title = audio.get('title', [None])[0]
                    existing_album = audio.get('album', [None])[0]
                if existing_title == title and existing_album == album:
                    return True
            except Exception:
                continue
        return False

    def __set_quality(self) -> None:
        self.__file_format = self.__c_quality['f_format']
        self.__song_quality = self.__c_quality['s_quality']

    def __set_song_path(self) -> None:
        # If the Preferences object has custom formatting strings, pass them on.
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

    def easy_dw(self) -> Track:
        if self.__infos_dw.get('__TYPE__') == 'episode':
            pic = self.__infos_dw.get('EPISODE_IMAGE_MD5', '')
        else:
            pic = self.__infos_dw['ALB_PICTURE']
        image = API.choose_img(pic)
        self.__song_metadata['image'] = image
        song = f"{self.__song_metadata['music']} - {self.__song_metadata['artist']}"

        # Check if track already exists based on metadata
        current_title = self.__song_metadata['music']
        current_album = self.__song_metadata['album']
        if self.__track_already_exists(current_title, current_album):
            # Create skipped progress report using the new required format
            progress_data = {
                "type": "track",
                "song": current_title,
                "artist": self.__song_metadata['artist'],
                "status": "skipped",
                "url": self.__link,
                "reason": "Track already exists",
                "convert_to": self.__convert_to
            }
            
            # Add parent info based on parent type
            if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                playlist_data = self.__preferences.json_data
                playlist_name = playlist_data.get('title', 'unknown')
                total_tracks = getattr(self.__preferences, 'total_tracks', 0)
                current_track = getattr(self.__preferences, 'track_number', 0)
                
                # Format for playlist-parented tracks exactly as required
                progress_data.update({
                    "current_track": current_track,
                    "total_tracks": total_tracks,
                    "parent": {
                        "type": "playlist",
                        "name": playlist_name,
                        "owner": playlist_data.get('creator', {}).get('name', 'unknown'),
                        "total_tracks": total_tracks,
                        "url": f"https://deezer.com/playlist/{self.__preferences.json_data.get('id', '')}"
                    }
                })
            elif self.__parent == "album":
                album_name = self.__song_metadata.get('album', '')
                album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('album_artist', ''))
                total_tracks = getattr(self.__preferences, 'total_tracks', 0)
                current_track = getattr(self.__preferences, 'track_number', 0)
                
                # Format for album-parented tracks exactly as required
                progress_data.update({
                    "current_track": current_track,
                    "total_tracks": total_tracks,
                    "parent": {
                        "type": "album",
                        "title": album_name,
                        "artist": album_artist,
                        "total_tracks": total_tracks,
                        "url": f"https://deezer.com/album/{self.__preferences.song_metadata.get('album_id', '')}"
                    }
                })
        
            Download_JOB.report_progress(progress_data)
            # self.__c_track might not be fully initialized here if __write_track() hasn't been called
            # Create a minimal track object for skipped scenario
            skipped_item = Track(
                self.__song_metadata,
                self.__song_path, # song_path would be set if __write_track was called
                self.__file_format, self.__song_quality,
                self.__link, self.__ids
            )
            skipped_item.success = False
            skipped_item.was_skipped = True
            # It's important that this skipped_item is what's checked later, or self.__c_track is updated
            self.__c_track = skipped_item # Ensure self.__c_track reflects this skipped state
            return self.__c_track # Return the correctly flagged skipped track

        # Initialize success to False for the item being processed
        if self.__infos_dw.get('__TYPE__') == 'episode':
            if hasattr(self, '_EASY_DW__c_episode') and self.__c_episode:
                 self.__c_episode.success = False
        else:
            if hasattr(self, '_EASY_DW__c_track') and self.__c_track:
                 self.__c_track.success = False

        try:
            if self.__infos_dw.get('__TYPE__') == 'episode':
                # download_episode_try should set self.__c_episode.success = True if successful
                self.download_episode_try() # This will modify self.__c_episode directly
            else:
                # download_try should set self.__c_track.success = True if successful
                self.download_try() # This will modify self.__c_track directly
                
                # Create done status report using the new required format (only if download_try didn't fail)
                # This part should only execute if download_try itself was successful (i.e., no exception)
                if self.__c_track.success : # Check if download_try marked it as successful
                    progress_data = {
                        "type": "track",
                        "song": self.__song_metadata['music'],
                        "artist": self.__song_metadata['artist'],
                        "status": "done",
                        "convert_to": self.__convert_to
                    }
                    spotify_url = getattr(self.__preferences, 'spotify_url', None)
                    progress_data["url"] = spotify_url if spotify_url else self.__link
                    if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                        playlist_data = self.__preferences.json_data
                        # ... (rest of playlist parent data) ...
                        progress_data.update({
                            "current_track": getattr(self.__preferences, 'track_number', 0),
                            "total_tracks": getattr(self.__preferences, 'total_tracks', 0),
                            "parent": {
                                "type": "playlist",
                                "name": playlist_data.get('title', 'unknown'),
                                "owner": playlist_data.get('creator', {}).get('name', 'unknown')
                            }
                        })
                elif self.__parent == "album":
                    album_name = self.__song_metadata.get('album', '')
                    album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('album_artist', ''))
                    total_tracks = getattr(self.__preferences, 'total_tracks', 0)
                    current_track = getattr(self.__preferences, 'track_number', 0)
                    
                    progress_data.update({
                        "current_track": current_track,
                        "total_tracks": total_tracks,
                        "parent": {
                            "type": "album",
                            "title": album_name,
                            "artist": album_artist,
                            "total_tracks": total_tracks,
                            "url": f"https://deezer.com/album/{self.__preferences.song_metadata.get('album_id', '')}"
                        }
                    })
                    Download_JOB.report_progress(progress_data)

        except Exception as e: # Covers failures within download_try or download_episode_try
            item_type = "Episode" if self.__infos_dw.get('__TYPE__') == 'episode' else "Track"
            item_name = self.__song_metadata.get('music', f'Unknown {item_type}')
            artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
            error_message = f"Download process failed for {item_type.lower()} '{item_name}' by '{artist_name}' (URL: {self.__link}). Error: {str(e)}"
            logger.error(error_message)
            
            current_item_obj = self.__c_episode if self.__infos_dw.get('__TYPE__') == 'episode' else self.__c_track
            if current_item_obj:
                current_item_obj.success = False
                current_item_obj.error_message = error_message
            raise TrackNotFound(message=error_message, url=self.__link) from e

        # --- Handling after download attempt --- 

        current_item = self.__c_episode if self.__infos_dw.get('__TYPE__') == 'episode' else self.__c_track
        item_type_str = "episode" if self.__infos_dw.get('__TYPE__') == 'episode' else "track"

        # If the item was skipped (e.g. file already exists), return it immediately.
        if getattr(current_item, 'was_skipped', False):
            return current_item

        # Final check for non-skipped items that might have failed.
        if not current_item.success:
            item_name = self.__song_metadata.get('music', f'Unknown {item_type_str.capitalize()}')
            artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
            original_error_msg = getattr(current_item, 'error_message', f"Download failed for an unspecified reason after {item_type_str} processing attempt.")
            error_msg_template = "Cannot download {type} '{title}' by '{artist}'. Reason: {reason}"
            final_error_msg = error_msg_template.format(type=item_type_str, title=item_name, artist=artist_name, reason=original_error_msg)
            current_link_attr = current_item.link if hasattr(current_item, 'link') and current_item.link else self.__link
            logger.error(f"{final_error_msg} (URL: {current_link_attr})")
            current_item.error_message = final_error_msg
            raise TrackNotFound(message=final_error_msg, url=current_link_attr)

        # If we reach here, the item should be successful and not skipped.
        if current_item.success:
            if self.__infos_dw.get('__TYPE__') != 'episode': # Assuming pic is for tracks
                 current_item.md5_image = pic # Set md5_image for tracks
            write_tags(current_item)
        
        return current_item

    def download_try(self) -> Track:
        # Pre-check: if FLAC is requested but filesize is zero, fallback to MP3.
        if self.__file_format == '.flac':
            filesize_str = self.__infos_dw.get('FILESIZE_FLAC', '0')
            try:
                filesize = int(filesize_str)
            except ValueError:
                filesize = 0

            if filesize == 0:
                song = self.__song_metadata['music']
                artist = self.__song_metadata['artist']
                # Switch quality settings to MP3_320.
                self.__quality_download = 'MP3_320'
                self.__file_format = '.mp3'
                self.__song_path = self.__song_path.rsplit('.', 1)[0] + '.mp3'
                media = Download_JOB.check_sources([self.__infos_dw], 'MP3_320')
                if media:
                    self.__infos_dw['media_url'] = media[0]
                else:
                    raise TrackNotFound(f"Track {song} - {artist} not available in MP3 format after FLAC attempt failed (filesize was 0).")

        # Continue with the normal download process.
        try:
            media_list = self.__infos_dw['media_url']['media']
            song_link = media_list[0]['sources'][0]['url']

            try:
                crypted_audio = API_GW.song_exist(song_link)
            except TrackNotFound:
                song = self.__song_metadata['music']
                artist = self.__song_metadata['artist']

                if self.__file_format == '.flac':
                    logger.warning(f"\n⚠ {song} - {artist} is not available in FLAC format. Trying MP3...")
                    self.__quality_download = 'MP3_320'
                    self.__file_format = '.mp3'
                    self.__song_path = self.__song_path.rsplit('.', 1)[0] + '.mp3'

                    media = Download_JOB.check_sources(
                        [self.__infos_dw], 'MP3_320'
                    )
                    if media:
                        self.__infos_dw['media_url'] = media[0]
                        song_link = media[0]['media'][0]['sources'][0]['url']
                        crypted_audio = API_GW.song_exist(song_link)
                    else:
                        raise TrackNotFound(f"Track {song} - {artist} not available in MP3 after FLAC attempt failed (media not found for MP3).")
                else:
                    if not self.__recursive_quality:
                        # msg was not defined, provide a more specific message
                        raise QualityNotFound(f"Quality {self.__quality_download} not found for {song} - {artist} and recursive quality search is disabled.")
                    for c_quality in qualities:
                        if self.__quality_download == c_quality:
                            continue
                        media = Download_JOB.check_sources(
                            [self.__infos_dw], c_quality
                        )
                        if media:
                            self.__infos_dw['media_url'] = media[0]
                            song_link = media[0]['media'][0]['sources'][0]['url']
                            try:
                                crypted_audio = API_GW.song_exist(song_link)
                                self.__c_quality = qualities[c_quality]
                                self.__set_quality()
                                break
                            except TrackNotFound:
                                if c_quality == "MP3_128":
                                    raise TrackNotFound(f"Error with {song} - {artist}. All available qualities failed, last attempt was {c_quality}. Link: {self.__link}")
                                continue

            c_crypted_audio = crypted_audio.iter_content(2048)
            
            # Get track IDs and encryption information
            # The enhanced check_track_ids function will determine the encryption type
            self.__fallback_ids = check_track_ids(self.__infos_dw)
            encryption_type = self.__fallback_ids.get('encryption_type', 'unknown')
            logger.debug(f"Using encryption type: {encryption_type}")

            try:
                self.__write_track()
                
                # Send immediate progress status for the track
                progress_data = {
                    "type": "track",
                    "song": self.__song_metadata.get("music", ""),
                    "artist": self.__song_metadata.get("artist", ""),
                    "status": "progress"
                }
                
                # Use Spotify URL if available, otherwise use Deezer link
                spotify_url = getattr(self.__preferences, 'spotify_url', None)
                progress_data["url"] = spotify_url if spotify_url else self.__link
                
                # Add parent info if present
                if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                    playlist_data = self.__preferences.json_data
                    playlist_name = playlist_data.get('title', 'unknown')
                    total_tracks = getattr(self.__preferences, 'total_tracks', 0)
                    current_track = getattr(self.__preferences, 'track_number', 0)
                    
                    progress_data.update({
                        "current_track": current_track,
                        "total_tracks": total_tracks,
                        "parent": {
                            "type": "playlist",
                            "name": playlist_name,
                            "owner": playlist_data.get('creator', {}).get('name', 'unknown'),
                            "total_tracks": total_tracks,
                            "url": f"https://deezer.com/playlist/{self.__preferences.json_data.get('id', '')}"
                        }
                    })
                elif self.__parent == "album":
                    album_name = self.__song_metadata.get('album', '')
                    album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('album_artist', ''))
                    total_tracks = getattr(self.__preferences, 'total_tracks', 0)
                    current_track = getattr(self.__preferences, 'track_number', 0)
                    
                    progress_data.update({
                        "current_track": current_track,
                        "total_tracks": total_tracks,
                        "parent": {
                            "type": "album",
                            "title": album_name,
                            "artist": album_artist,
                            "total_tracks": total_tracks,
                            "url": f"https://deezer.com/album/{self.__preferences.song_metadata.get('album_id', '')}"
                        }
                    })
                
                Download_JOB.report_progress(progress_data)
                
                try:
                    # Decrypt the file using the utility function
                    decryptfile(c_crypted_audio, self.__fallback_ids, self.__song_path)
                    logger.debug(f"Successfully decrypted track using {encryption_type} encryption")
                except Exception as decrypt_error:
                    # Detailed error logging for debugging
                    logger.error(f"Decryption error ({encryption_type}): {str(decrypt_error)}")
                    if "Data must be padded" in str(decrypt_error):
                        logger.error("This appears to be a padding issue with Blowfish decryption")
                    raise
                
                self.__add_more_tags()
                
                # Apply audio conversion if requested
                if self.__convert_to:
                    format_name, bitrate = parse_format_string(self.__convert_to)
                    if format_name:
                        # Register and unregister functions for tracking downloads
                        from deezspot.deezloader.__download__ import register_active_download, unregister_active_download
                        try:
                            # Update the path with the converted file path
                            converted_path = convert_audio(
                                self.__song_path, 
                                format_name, 
                                bitrate,
                                register_active_download,
                                unregister_active_download
                            )
                            if converted_path != self.__song_path:
                                # Update path in track object if conversion happened
                                self.__song_path = converted_path
                                self.__c_track.song_path = converted_path
                        except Exception as conv_error:
                            # Log conversion error but continue with original file
                            logger.error(f"Audio conversion error: {str(conv_error)}")
                
                # Write tags to the final file (original or converted)
                write_tags(self.__c_track)
            except Exception as e:
                if isfile(self.__song_path):
                    os.remove(self.__song_path)
                
                # Improve error message formatting
                error_msg = str(e)
                if "Data must be padded" in error_msg:
                    error_msg = "Decryption error (padding issue) - Try a different quality setting or download format"
                elif isinstance(e, ConnectionError) or "Connection" in error_msg:
                    error_msg = "Connection error - Check your internet connection"
                elif "timeout" in error_msg.lower():
                    error_msg = "Request timed out - Server may be busy"
                elif "403" in error_msg or "Forbidden" in error_msg:
                    error_msg = "Access denied - Track might be region-restricted or premium-only"
                elif "404" in error_msg or "Not Found" in error_msg:
                    error_msg = "Track not found - It might have been removed"
                
                # Create formatted error report
                progress_data = {
                    "type": "track",
                    "status": "error",
                    "song": self.__song_metadata.get('music', ''),
                    "artist": self.__song_metadata.get('artist', ''),
                    "error": error_msg,
                    "url": getattr(self.__preferences, 'spotify_url', None) or self.__link,
                    "convert_to": self.__convert_to
                }
                
                # Add parent info based on parent type
                if self.__parent == "playlist" and hasattr(self.__preferences, "json_data"):
                    playlist_data = self.__preferences.json_data
                    playlist_name = playlist_data.get('title', 'unknown')
                    total_tracks = getattr(self.__preferences, 'total_tracks', 0)
                    current_track = getattr(self.__preferences, 'track_number', 0)
                    
                    progress_data.update({
                        "current_track": current_track,
                        "total_tracks": total_tracks,
                        "parent": {
                            "type": "playlist",
                            "name": playlist_name,
                            "owner": playlist_data.get('creator', {}).get('name', 'unknown'),
                            "total_tracks": total_tracks,
                            "url": f"https://deezer.com/playlist/{playlist_data.get('id', '')}"
                        }
                    })
                elif self.__parent == "album":
                    album_name = self.__song_metadata.get('album', '')
                    album_artist = self.__song_metadata.get('album_artist', self.__song_metadata.get('album_artist', ''))
                    total_tracks = getattr(self.__preferences, 'total_tracks', 0)
                    current_track = getattr(self.__preferences, 'track_number', 0)
                    
                    progress_data.update({
                        "current_track": current_track,
                        "total_tracks": total_tracks,
                        "parent": {
                            "type": "album",
                            "title": album_name,
                            "artist": album_artist,
                            "total_tracks": total_tracks,
                            "url": f"https://deezer.com/album/{self.__preferences.song_metadata.get('album_id', '')}"
                        }
                    })
                
                # Report the error
                Download_JOB.report_progress(progress_data)
                logger.error(f"Failed to process track: {error_msg}")
                
                # Still raise the exception to maintain original flow
                # Add the original exception e to the message for more context
                self.__c_track.success = False # Mark as failed
                self.__c_track.error_message = error_msg # Store the refined error message
                raise TrackNotFound(f"Failed to process {self.__song_path}. Error: {error_msg}. Original Exception: {str(e)}")

            # If download and processing (like decryption, tagging) were successful before conversion
            if not self.__convert_to: # Or if conversion was successful
                 self.__c_track.success = True

            return self.__c_track

        except Exception as e:
            # Add more context to this exception
            song_title = self.__song_metadata.get('music', 'Unknown Song')
            artist_name = self.__song_metadata.get('artist', 'Unknown Artist')
            error_message = f"Download failed for '{song_title}' by '{artist_name}' (Link: {self.__link}). Error: {str(e)}"
            logger.error(error_message)
            # Store error on track object if possible
            if hasattr(self, '_EASY_DW__c_track') and self.__c_track:
                self.__c_track.success = False
                self.__c_track.error_message = str(e)
            raise TrackNotFound(message=error_message, url=self.__link) from e

    def download_episode_try(self) -> Episode:
        try:
            direct_url = self.__infos_dw.get('EPISODE_DIRECT_STREAM_URL')
            if not direct_url:
                raise TrackNotFound("No direct stream URL found")

            os.makedirs(os.path.dirname(self.__song_path), exist_ok=True)

            response = requests.get(direct_url, stream=True)
            response.raise_for_status()

            content_length = response.headers.get('content-length')
            total_size = int(content_length) if content_length else None

            downloaded = 0
            with open(self.__song_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        size = f.write(chunk)
                        downloaded += size
                        
                        # Download progress reporting could be added here

            # Build episode progress report
            progress_data = {
                "type": "episode",
                "song": self.__song_metadata.get('music', 'Unknown Episode'),
                "artist": self.__song_metadata.get('artist', 'Unknown Show'),
                "status": "done"
            }
            
            # Use Spotify URL if available (for downloadspo functions), otherwise use Deezer link
            spotify_url = getattr(self.__preferences, 'spotify_url', None)
            progress_data["url"] = spotify_url if spotify_url else self.__link
            
            Download_JOB.report_progress(progress_data)
            
            self.__c_track.success = True
            self.__write_episode()
            write_tags(self.__c_track)
        
            return self.__c_track

        except Exception as e:
            if isfile(self.__song_path):
                os.remove(self.__song_path)
            self.__c_track.success = False
            episode_title = self.__preferences.song_metadata.get('music', 'Unknown Episode')
            err_msg = f"Episode download failed for '{episode_title}' (URL: {self.__link}). Error: {str(e)}"
            logger.error(err_msg)
            # Store error on track object
            self.__c_track.error_message = str(e)
            raise TrackNotFound(message=err_msg, url=self.__link) from e

    def __add_more_tags(self) -> None:
        contributors = self.__infos_dw.get('SNG_CONTRIBUTORS', {})

        if "author" in contributors:
            self.__song_metadata['author'] = "; ".join(
                contributors['author']
            )
        else:
            self.__song_metadata['author'] = ""

        if "composer" in contributors:
            self.__song_metadata['composer'] = "; ".join(
                contributors['composer']
            )
        else:
            self.__song_metadata['composer'] = ""

        if "lyricist" in contributors:
            self.__song_metadata['lyricist'] = "; ".join(
                contributors['lyricist']
            )
        else:
            self.__song_metadata['lyricist'] = ""

        if "composerlyricist" in contributors:
            self.__song_metadata['composer'] = "; ".join(
                contributors['composerlyricist']
            )
        else:
            self.__song_metadata['composerlyricist'] = ""

        if "version" in self.__infos_dw:
            self.__song_metadata['version'] = self.__infos_dw['VERSION']
        else:
            self.__song_metadata['version'] = ""

        self.__song_metadata['lyric'] = ""
        self.__song_metadata['copyright'] = ""
        self.__song_metadata['lyricist'] = ""
        self.__song_metadata['lyric_sync'] = []

        if self.__infos_dw.get('LYRICS_ID', 0) != 0:
            need = API_GW.get_lyric(self.__ids)

            if "LYRICS_SYNC_JSON" in need:
                self.__song_metadata['lyric_sync'] = trasform_sync_lyric(
                    need['LYRICS_SYNC_JSON']
                )

        # This method should only add tags. Error handling for download success/failure
        # is managed by easy_dw after calls to download_try/download_episode_try.
        # No error re-raising or success flag modification here.
        # write_tags is called after this in download_try if successful.

class DW_TRACK:
    def __init__(
        self,
        preferences: Preferences
    ) -> None:

        self.__preferences = preferences
        self.__ids = self.__preferences.ids
        self.__song_metadata = self.__preferences.song_metadata
        self.__quality_download = self.__preferences.quality_download

    def dw(self) -> Track:
        infos_dw = API_GW.get_song_data(self.__ids)

        media = Download_JOB.check_sources(
            [infos_dw], self.__quality_download
        )

        infos_dw['media_url'] = media[0]

        # For individual tracks, parent is None (not part of album or playlist)
        track = EASY_DW(infos_dw, self.__preferences, parent=None).easy_dw()

        # Check if track failed but was NOT intentionally skipped
        if not track.success and not getattr(track, 'was_skipped', False):
            song = f"{self.__song_metadata['music']} - {self.__song_metadata['artist']}"
            # Attempt to get the original error message if available from the track object
            original_error = getattr(track, 'error_message', "it's not available in this format or an error occurred.")
            error_msg = f"Cannot download '{song}'. Reason: {original_error}"
            current_link = track.link if hasattr(track, 'link') and track.link else self.__preferences.link
            logger.error(f"{error_msg} (Link: {current_link})")
            raise TrackNotFound(message=error_msg, url=current_link)

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
        self.__quality_download = self.__preferences.quality_download

        self.__song_metadata_items = self.__song_metadata.items()

    def dw(self) -> Album:
        # Helper function to find most frequent item in a list
        def most_frequent(items):
            if not items:
                return None
            return max(set(items), key=items.count)
        
        # Derive album_artist strictly from the album's API contributors
        album_api_contributors = self.__preferences.json_data.get('contributors', [])
        derived_album_artist_from_contributors = "Unknown Artist" # Default

        if album_api_contributors: # Check if contributors list is not empty
            main_contributor_names = [
                c.get('name') for c in album_api_contributors
                if c.get('name') and c.get('role', '').lower() == 'main'
            ]

            if main_contributor_names:
                derived_album_artist_from_contributors = "; ".join(main_contributor_names)
            else: # No 'Main' contributors, try all contributors with a name
                all_contributor_names = [
                    c.get('name') for c in album_api_contributors if c.get('name')
                ]
                if all_contributor_names:
                    derived_album_artist_from_contributors = "; ".join(all_contributor_names)
        # If album_api_contributors is empty or no names were found, it remains "Unknown Artist"


        # Report album initializing status
        album_name_for_report = self.__song_metadata.get('album', 'Unknown Album')
        total_tracks_for_report = self.__song_metadata.get('nb_tracks', 0)
        
        Download_JOB.report_progress({
            "type": "album",
            "artist": derived_album_artist_from_contributors,
            "status": "initializing",
            "total_tracks": total_tracks_for_report,
            "title": album_name_for_report,
            "url": f"https://deezer.com/album/{self.__ids}"
        })
        
        infos_dw = API_GW.get_album_data(self.__ids)['data']

        md5_image = infos_dw[0]['ALB_PICTURE']
        image = API.choose_img(md5_image)
        self.__song_metadata['image'] = image

        album = Album(self.__ids)
        album.image = image
        album.md5_image = md5_image
        album.nb_tracks = self.__song_metadata['nb_tracks']
        album.album_name = self.__song_metadata['album']
        album.upc = self.__song_metadata['upc']
        tracks = album.tracks
        album.tags = self.__song_metadata

        # Get media URLs using the splitting approach
        medias = Download_JOB.check_sources(
            infos_dw, self.__quality_download
        )
        
        # The album_artist for tagging individual tracks will be derived_album_artist_from_contributors
        
        total_tracks = len(infos_dw)
        for a in range(total_tracks):
            track_number = a + 1
            c_infos_dw = infos_dw[a]
            
            # Retrieve the contributors info from the API response.
            # It might be an empty list.
            contributors = c_infos_dw.get('SNG_CONTRIBUTORS', {})
            
            # Check if contributors is an empty list.
            if isinstance(contributors, list) and not contributors:
                # Flag indicating we do NOT have contributors data to process.
                has_contributors = False
            else:
                has_contributors = True

            # If we have contributor data, build the artist and composer strings.
            if has_contributors:
                main_artist = "; ".join(contributors.get('main_artist', []))
                featuring = "; ".join(contributors.get('featuring', []))
                
                artist_parts = [main_artist]
                if featuring:
                    artist_parts.append(f"(feat. {featuring})")
                artist_str = " ".join(artist_parts)
                composer_str = "; ".join(contributors.get('composer', []))
            
            # Build the core track metadata.
            # When there is no contributor info, we intentionally leave out the 'artist'
            # and 'composer' keys so that the album-level metadata merge will supply them.
            c_song_metadata = {
                'music': c_infos_dw.get('SNG_TITLE', 'Unknown'),
                'album': self.__song_metadata['album'],
                'date': c_infos_dw.get('DIGITAL_RELEASE_DATE', ''),
                'genre': self.__song_metadata.get('genre', 'Latin Music'),
                'tracknum': f"{track_number}",
                'discnum': f"{c_infos_dw.get('DISK_NUMBER', 1)}",
                'isrc': c_infos_dw.get('ISRC', ''),
                'album_artist': derived_album_artist_from_contributors,
                'publisher': 'CanZion R',
                'duration': int(c_infos_dw.get('DURATION', 0)),
                'explicit': '1' if c_infos_dw.get('EXPLICIT_LYRICS', '0') == '1' else '0'
            }
            
            # Only add contributor-based metadata if available.
            if has_contributors:
                c_song_metadata['artist'] = artist_str
                c_song_metadata['composer'] = composer_str

            # No progress reporting here - done at the track level
            
            # Merge album-level metadata (only add fields not already set in c_song_metadata)
            for key, item in self.__song_metadata_items:
                if key not in c_song_metadata:
                    if isinstance(item, list):
                        c_song_metadata[key] = self.__song_metadata[key][a] if len(self.__song_metadata[key]) > a else 'Unknown'
                    else:
                        c_song_metadata[key] = self.__song_metadata[key]
            
            # Continue with the rest of your processing (media handling, download, etc.)
            c_infos_dw['media_url'] = medias[a]
            c_preferences = deepcopy(self.__preferences)
            c_preferences.song_metadata = c_song_metadata.copy()
            c_preferences.ids = c_infos_dw['SNG_ID']
            c_preferences.track_number = track_number
            
            # Add additional information for consistent parent info
            c_preferences.song_metadata['album_id'] = self.__ids
            c_preferences.song_metadata['total_tracks'] = total_tracks
            c_preferences.total_tracks = total_tracks
            c_preferences.link = f"https://deezer.com/track/{c_preferences.ids}"
            
            try:
                track = EASY_DW(c_infos_dw, c_preferences, parent='album').download_try()
            except TrackNotFound:
                try:
                    song = f"{c_song_metadata['music']} - {c_song_metadata.get('artist', self.__song_metadata['artist'])}"
                    ids = API.not_found(song, c_song_metadata['music'])
                    c_infos_dw = API_GW.get_song_data(ids)
                    c_media = Download_JOB.check_sources([c_infos_dw], self.__quality_download)
                    c_infos_dw['media_url'] = c_media[0]
                    track = EASY_DW(c_infos_dw, c_preferences, parent='album').download_try()
                except TrackNotFound:
                    track = Track(c_song_metadata, None, None, None, None, None)
                    track.success = False
                    track.error_message = f"Track not found after fallback attempt for: {song}" 
                    logger.warning(f"Track not found: {song} :( Details: {track.error_message}. URL: {c_preferences.link if c_preferences else 'N/A'}")
            tracks.append(track)

        if self.__make_zip:
            song_quality = tracks[0].quality if tracks else 'Unknown'
            # Pass along custom directory format if set
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
        total_tracks = self.__song_metadata.get('nb_tracks', 0)
        
        Download_JOB.report_progress({
            "type": "album",
            "artist": derived_album_artist_from_contributors,
            "status": "done",
            "total_tracks": total_tracks,
            "title": album_name,
            "url": f"https://deezer.com/album/{self.__ids}"
        })
        
        return album

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
        self.__quality_download = self.__preferences.quality_download

    def dw(self) -> Playlist:
        # Extract playlist metadata for reporting
        playlist_name = self.__json_data.get('title', 'Unknown Playlist')
        playlist_owner = self.__json_data.get('creator', {}).get('name', 'Unknown Owner')
        total_tracks = self.__json_data.get('nb_tracks', 0)
        
        # Report playlist initializing status
        Download_JOB.report_progress({
            "type": "playlist",
            "owner": playlist_owner,
            "status": "initializing",
            "total_tracks": total_tracks,
            "name": playlist_name,
            "url": f"https://deezer.com/playlist/{self.__ids}"
        })
        
        # Retrieve playlist data from API
        infos_dw = API_GW.get_playlist_data(self.__ids)['data']
        
        # Extract playlist metadata - we'll use this in the track-level reporting
        playlist_name = self.__json_data['title']
        total_tracks = len(infos_dw)

        playlist = Playlist()
        tracks = playlist.tracks

        # --- Prepare the m3u playlist file ---
        # m3u file will be placed in output_dir/playlists
        playlist_m3u_dir = os.path.join(self.__output_dir, "playlists")
        os.makedirs(playlist_m3u_dir, exist_ok=True)
        m3u_path = os.path.join(playlist_m3u_dir, f"{playlist_name}.m3u")
        if not os.path.exists(m3u_path):
            with open(m3u_path, "w", encoding="utf-8") as m3u_file:
                m3u_file.write("#EXTM3U\n")
        # -------------------------------------

        # Get media URLs for each track in the playlist
        medias = Download_JOB.check_sources(
            infos_dw, self.__quality_download
        )

        # Process each track
        for idx, (c_infos_dw, c_media, c_song_metadata) in enumerate(zip(infos_dw, medias, self.__song_metadata), 1):

            # Skip if song metadata is not valid
            if type(c_song_metadata) is str:
                continue

            c_infos_dw['media_url'] = c_media
            c_preferences = deepcopy(self.__preferences)
            c_preferences.ids = c_infos_dw['SNG_ID']
            c_preferences.song_metadata = c_song_metadata
            c_preferences.track_number = idx
            c_preferences.total_tracks = total_tracks

            # Download the track using the EASY_DW downloader
            track = EASY_DW(c_infos_dw, c_preferences, parent='playlist').easy_dw()

            # Track-level progress reporting is handled in EASY_DW

            # Only log a warning if the track failed and was NOT intentionally skipped
            if not track.success and not getattr(track, 'was_skipped', False):
                song = f"{c_song_metadata['music']} - {c_song_metadata['artist']}"
                error_detail = getattr(track, 'error_message', 'Download failed for unspecified reason.')
                logger.warning(f"Cannot download '{song}'. Reason: {error_detail} (Link: {track.link or c_preferences.link})")

            tracks.append(track)

            # --- Append the final track path to the m3u file ---
            # Build a relative path from the playlists directory
            if track.success and hasattr(track, 'song_path') and track.song_path:
                relative_song_path = os.path.relpath(
                    track.song_path,
                    start=os.path.join(self.__output_dir, "playlists")
                )
                with open(m3u_path, "a", encoding="utf-8") as m3u_file:
                    m3u_file.write(f"{relative_song_path}\n")
            # --------------------------------------------------

        if self.__make_zip:
            playlist_title = self.__json_data['title']
            zip_name = f"{self.__output_dir}/{playlist_title} [playlist {self.__ids}]"
            create_zip(tracks, zip_name=zip_name)
            playlist.zip_path = zip_name

        # Report playlist done status
        playlist_name = self.__json_data.get('title', 'Unknown Playlist')
        playlist_owner = self.__json_data.get('creator', {}).get('name', 'Unknown Owner')
        total_tracks = self.__json_data.get('nb_tracks', 0)
        
        Download_JOB.report_progress({
            "type": "playlist",
            "owner": playlist_owner,
            "status": "done",
            "total_tracks": total_tracks,
            "name": playlist_name,
            "url": f"https://deezer.com/playlist/{self.__ids}"
        })
        
        return playlist

class DW_EPISODE:
    def __init__(
        self,
        preferences: Preferences
    ) -> None:
        self.__preferences = preferences
        self.__ids = preferences.ids
        self.__output_dir = preferences.output_dir
        self.__method_save = preferences.method_save
        self.__not_interface = preferences.not_interface
        self.__quality_download = preferences.quality_download
        
    def dw(self) -> Track:
        infos_dw = API_GW.get_episode_data(self.__ids)
        infos_dw['__TYPE__'] = 'episode'
        
        self.__preferences.song_metadata = {
            'music': infos_dw.get('EPISODE_TITLE', ''),
            'artist': infos_dw.get('SHOW_NAME', ''),
            'album': infos_dw.get('SHOW_NAME', ''),
            'date': infos_dw.get('EPISODE_PUBLISHED_TIMESTAMP', '').split()[0],
            'genre': 'Podcast',
            'explicit': infos_dw.get('SHOW_IS_EXPLICIT', '2'),
            'duration': int(infos_dw.get('DURATION', 0)),
        }
        
        try:
            direct_url = infos_dw.get('EPISODE_DIRECT_STREAM_URL')
            if not direct_url:
                raise TrackNotFound("No direct URL found")
            
            from deezspot.libutils.utils import sanitize_name
            from pathlib import Path
            safe_filename = sanitize_name(self.__preferences.song_metadata['music'])
            Path(self.__output_dir).mkdir(parents=True, exist_ok=True)
            output_path = os.path.join(self.__output_dir, f"{safe_filename}.mp3")
            
            response = requests.get(direct_url, stream=True)
            response.raise_for_status()

            content_length = response.headers.get('content-length')
            total_size = int(content_length) if content_length else None

            downloaded = 0
            total_size = int(response.headers.get('content-length', 0))
            
            # Send initial progress status
            progress_data = {
                "type": "episode",
                "song": self.__preferences.song_metadata.get('name', ''),
                "artist": self.__preferences.song_metadata.get('publisher', ''),
                "status": "progress",
                "url": f"https://www.deezer.com/episode/{self.__ids}",
                "parent": {
                    "type": "show",
                    "title": self.__preferences.song_metadata.get('show', ''),
                    "artist": self.__preferences.song_metadata.get('publisher', '')
                }
            }
            Download_JOB.report_progress(progress_data)
            
            with open(output_path, 'wb') as f:
                start_time = time.time()
                last_report_time = 0
                
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        size = f.write(chunk)
                        downloaded += size
                        
                        # Real-time progress reporting every 0.5 seconds
                        current_time = time.time()
                        if self.__real_time_dl and total_size > 0 and current_time - last_report_time >= 0.5:
                            last_report_time = current_time
                            percentage = round((downloaded / total_size) * 100, 2)
                            
                            progress_data = {
                                "type": "episode",
                                "song": self.__preferences.song_metadata.get('name', ''),
                                "artist": self.__preferences.song_metadata.get('publisher', ''),
                                "status": "real-time",
                                "url": f"https://www.deezer.com/episode/{self.__ids}",
                                "time_elapsed": int((current_time - start_time) * 1000),
                                "progress": percentage,
                                "parent": {
                                    "type": "show",
                                    "title": self.__preferences.song_metadata.get('show', ''),
                                    "artist": self.__preferences.song_metadata.get('publisher', '')
                                }
                            }
                            Download_JOB.report_progress(progress_data)
            
            episode = Track(
                self.__preferences.song_metadata,
                output_path,
                '.mp3',
                self.__quality_download, 
                f"https://www.deezer.com/episode/{self.__ids}",
                self.__ids
            )
            episode.success = True
            
            # Send completion status
            progress_data = {
                "type": "episode",
                "song": self.__preferences.song_metadata.get('name', ''),
                "artist": self.__preferences.song_metadata.get('publisher', ''),
                "status": "done",
                "url": f"https://www.deezer.com/episode/{self.__ids}",
                "parent": {
                    "type": "show",
                    "title": self.__preferences.song_metadata.get('show', ''),
                    "artist": self.__preferences.song_metadata.get('publisher', '')
                }
            }
            Download_JOB.report_progress(progress_data)
            
            return episode
            
        except Exception as e:
            if 'output_path' in locals() and os.path.exists(output_path):
                os.remove(output_path)
            episode_title = self.__preferences.song_metadata.get('music', 'Unknown Episode')
            err_msg = f"Episode download failed for '{episode_title}' (URL: {self.__preferences.link}). Error: {str(e)}"
            logger.error(err_msg)
            # Add original error to exception
            raise TrackNotFound(message=err_msg, url=self.__preferences.link) from e
