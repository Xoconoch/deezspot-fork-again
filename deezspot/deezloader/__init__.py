#!/usr/bin/python3
import os
import json
import logging
from deezspot.deezloader.dee_api import API
from deezspot.easy_spoty import Spo
from deezspot.deezloader.deegw_api import API_GW
from deezspot.deezloader.deezer_settings import stock_quality
from deezspot.models import (
    Track,
    Album,
    Playlist,
    Preferences,
    Smart,
    Episode,
)
from deezspot.deezloader.__download__ import (
    DW_TRACK,
    DW_ALBUM,
    DW_PLAYLIST,
    DW_EPISODE,
    Download_JOB,
)
from deezspot.exceptions import (
    InvalidLink,
    TrackNotFound,
    NoDataApi,
    AlbumNotFound,
)
from deezspot.libutils.utils import (
    create_zip,
    get_ids,
    link_is_valid,
    what_kind,
)
from deezspot.libutils.others_settings import (
    stock_output,
    stock_recursive_quality,
    stock_recursive_download,
    stock_not_interface,
    stock_zip,
    method_save,
)
from deezspot.libutils.logging_utils import ProgressReporter, logger

API()

# Create a logger for the deezspot library
logger = logging.getLogger('deezspot')

class DeeLogin:
    def __init__(
        self,
        arl=None,
        email=None,
        password=None,
        spotify_client_id=None,
        spotify_client_secret=None,
        progress_callback=None,
        silent=False
    ) -> None:

        # Store Spotify credentials
        self.spotify_client_id = spotify_client_id
        self.spotify_client_secret = spotify_client_secret
        
        # Initialize Spotify API if credentials are provided
        if spotify_client_id and spotify_client_secret:
            Spo.__init__(client_id=spotify_client_id, client_secret=spotify_client_secret)

        # Initialize Deezer API
        if arl:
            self.__gw_api = API_GW(arl=arl)
        else:
            self.__gw_api = API_GW(
                email=email,
                password=password
            )
            
        # Reference to the Spotify search functionality
        self.__spo = Spo
        
        # Configure progress reporting
        self.progress_reporter = ProgressReporter(callback=progress_callback, silent=silent)
        
        # Set the progress reporter for Download_JOB
        Download_JOB.set_progress_reporter(self.progress_reporter)

    def report_progress(self, progress_data):
        """Report progress using the configured reporter."""
        self.progress_reporter.report(progress_data)

    def download_trackdee(
        self, link_track,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5,
        convert_to=None
    ) -> Track:

        link_is_valid(link_track)
        ids = get_ids(link_track)

        try:
            song_metadata = API.tracking(ids)
        except NoDataApi:
            infos = self.__gw_api.get_song_data(ids)

            if not "FALLBACK" in infos:
                raise TrackNotFound(link_track)

            ids = infos['FALLBACK']['SNG_ID']
            song_metadata = API.tracking(ids)

        preferences = Preferences()
        preferences.link = link_track
        preferences.song_metadata = song_metadata
        preferences.quality_download = quality_download
        preferences.output_dir = output_dir
        preferences.ids = ids
        preferences.recursive_quality = recursive_quality
        preferences.recursive_download = recursive_download
        preferences.not_interface = not_interface
        preferences.method_save = method_save
        # New custom formatting preferences:
        preferences.custom_dir_format = custom_dir_format
        preferences.custom_track_format = custom_track_format
        # Track number padding option
        preferences.pad_tracks = pad_tracks
        # Retry parameters
        preferences.initial_retry_delay = initial_retry_delay
        preferences.retry_delay_increase = retry_delay_increase
        preferences.max_retries = max_retries
        # Audio conversion parameter
        preferences.convert_to = convert_to

        track = DW_TRACK(preferences).dw()

        return track

    def download_albumdee(
        self, link_album,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        make_zip=stock_zip,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5,
        convert_to=None
    ) -> Album:

        link_is_valid(link_album)
        ids = get_ids(link_album)

        try:
            album_json = API.get_album(ids)
        except NoDataApi:
            raise AlbumNotFound(link_album)

        song_metadata = API.tracking_album(album_json)

        preferences = Preferences()
        preferences.link = link_album
        preferences.song_metadata = song_metadata
        preferences.quality_download = quality_download
        preferences.output_dir = output_dir
        preferences.ids = ids
        preferences.json_data = album_json
        preferences.recursive_quality = recursive_quality
        preferences.recursive_download = recursive_download
        preferences.not_interface = not_interface
        preferences.method_save = method_save
        preferences.make_zip = make_zip
        # New custom formatting preferences:
        preferences.custom_dir_format = custom_dir_format
        preferences.custom_track_format = custom_track_format
        # Track number padding option
        preferences.pad_tracks = pad_tracks
        # Retry parameters
        preferences.initial_retry_delay = initial_retry_delay
        preferences.retry_delay_increase = retry_delay_increase
        preferences.max_retries = max_retries
        # Audio conversion parameter
        preferences.convert_to = convert_to

        album = DW_ALBUM(preferences).dw()

        return album

    def download_playlistdee(
        self, link_playlist,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        make_zip=stock_zip,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5,
        convert_to=None
    ) -> Playlist:

        link_is_valid(link_playlist)
        ids = get_ids(link_playlist)

        song_metadata = []
        playlist_json = API.get_playlist(ids)

        for track in playlist_json['tracks']['data']:
            c_ids = track['id']

            try:
                c_song_metadata = API.tracking(c_ids)
            except NoDataApi:
                infos = self.__gw_api.get_song_data(c_ids)
                if not "FALLBACK" in infos:
                    c_song_metadata = f"{track['title']} - {track['artist']['name']}"
                else:
                    c_song_metadata = API.tracking(c_ids)

            song_metadata.append(c_song_metadata)

        preferences = Preferences()
        preferences.link = link_playlist
        preferences.song_metadata = song_metadata
        preferences.quality_download = quality_download
        preferences.output_dir = output_dir
        preferences.ids = ids
        preferences.json_data = playlist_json
        preferences.recursive_quality = recursive_quality
        preferences.recursive_download = recursive_download
        preferences.not_interface = not_interface
        preferences.method_save = method_save
        preferences.make_zip = make_zip
        # New custom formatting preferences:
        preferences.custom_dir_format = custom_dir_format
        preferences.custom_track_format = custom_track_format
        # Track number padding option
        preferences.pad_tracks = pad_tracks
        # Retry parameters
        preferences.initial_retry_delay = initial_retry_delay
        preferences.retry_delay_increase = retry_delay_increase
        preferences.max_retries = max_retries
        # Audio conversion parameter
        preferences.convert_to = convert_to

        playlist = DW_PLAYLIST(preferences).dw()

        return playlist

    def download_artisttopdee(
        self, link_artist,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        convert_to=None
    ) -> list[Track]:

        link_is_valid(link_artist)
        ids = get_ids(link_artist)

        playlist_json = API.get_artist_top_tracks(ids)['data']

        names = [
            self.download_trackdee(
                track['link'], output_dir,
                quality_download, recursive_quality,
                recursive_download, not_interface,
                method_save=method_save,
                custom_dir_format=custom_dir_format,
                custom_track_format=custom_track_format,
                pad_tracks=pad_tracks,
                convert_to=convert_to
            )
            for track in playlist_json
        ]

        return names

    def convert_spoty_to_dee_link_track(self, link_track):
        link_is_valid(link_track)
        ids = get_ids(link_track)

        # Use stored credentials for API calls
        track_json = Spo.get_track(ids)
        external_ids = track_json['external_ids']

        if not external_ids:
            msg = f"⚠ The track \"{track_json['name']}\" can't be converted to Deezer link :( ⚠"
            raise TrackNotFound(
                url=link_track,
                message=msg
            )

        isrc = f"isrc:{external_ids['isrc']}"
        track_json_dee = API.get_track(isrc)
        track_link_dee = track_json_dee['link']

        return track_link_dee

    def download_trackspo(
        self, link_track,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5,
        convert_to=None
    ) -> Track:

        track_link_dee = self.convert_spoty_to_dee_link_track(link_track)

        track = self.download_trackdee(
            track_link_dee,
            output_dir=output_dir,
            quality_download=quality_download,
            recursive_quality=recursive_quality,
            recursive_download=recursive_download,
            not_interface=not_interface,
            method_save=method_save,
            custom_dir_format=custom_dir_format,
            custom_track_format=custom_track_format,
            pad_tracks=pad_tracks,
            initial_retry_delay=initial_retry_delay,
            retry_delay_increase=retry_delay_increase,
            max_retries=max_retries,
            convert_to=convert_to
        )

        return track

    def convert_spoty_to_dee_link_album(self, link_album):
        link_is_valid(link_album)
        ids = get_ids(link_album)
        link_dee = None

        spotify_album_data = Spo.get_album(ids)

        # Method 1: Try UPC
        try:
            external_ids = spotify_album_data.get('external_ids')
            if external_ids and 'upc' in external_ids:
                upc_base = str(external_ids['upc']).lstrip('0')
                if upc_base:
                    logger.debug(f"Attempting Deezer album search with UPC: {upc_base}")
                    try:
                        deezer_album_info = API.get_album(f"upc:{upc_base}")
                        if isinstance(deezer_album_info, dict) and 'link' in deezer_album_info:
                            link_dee = deezer_album_info['link']
                            logger.info(f"Found Deezer album via UPC: {link_dee}")
                    except NoDataApi:
                        logger.debug(f"No Deezer album found for UPC: {upc_base}")
                    except Exception as e_upc_search:
                        logger.warning(f"Error during Deezer API call for UPC {upc_base}: {e_upc_search}")
            else:
                logger.debug("No UPC found in Spotify data for album link conversion.")
        except Exception as e_upc_block:
            logger.error(f"Error processing UPC for album {link_album}: {e_upc_block}")

        # Method 2: Try ISRC if UPC failed
        if not link_dee:
            logger.debug(f"UPC method failed or skipped for {link_album}. Attempting ISRC method.")
            try:
                spotify_total_tracks = spotify_album_data.get('total_tracks')
                spotify_tracks_items = spotify_album_data.get('tracks', {}).get('items', [])

                if not spotify_tracks_items:
                    logger.warning(f"No track items in Spotify data for {link_album} to attempt ISRC lookup.")
                else:
                    for track_item in spotify_tracks_items:
                        try:
                            track_spotify_link = track_item.get('external_urls', {}).get('spotify')
                            if not track_spotify_link: continue

                            spotify_track_info = Spo.get_track(track_spotify_link)
                            isrc_value = spotify_track_info.get('external_ids', {}).get('isrc')
                            if not isrc_value: continue
                            
                            logger.debug(f"Attempting Deezer track search with ISRC: {isrc_value}")
                            deezer_track_info = API.get_track(f"isrc:{isrc_value}")

                            if isinstance(deezer_track_info, dict) and 'album' in deezer_track_info:
                                deezer_album_preview = deezer_track_info['album']
                                if isinstance(deezer_album_preview, dict) and 'id' in deezer_album_preview:
                                    deezer_album_id = deezer_album_preview['id']
                                    full_deezer_album_info = API.get_album(deezer_album_id)
                                    if (
                                        isinstance(full_deezer_album_info, dict) and
                                        full_deezer_album_info.get('nb_tracks') == spotify_total_tracks and
                                        'link' in full_deezer_album_info
                                    ):
                                        link_dee = full_deezer_album_info['link']
                                        logger.info(f"Found Deezer album via ISRC ({isrc_value}): {link_dee}")
                                        break  # Found a matching album, exit track loop
                        except NoDataApi:
                            logger.debug(f"No Deezer track/album found for ISRC: {isrc_value}")
                            # Continue to the next track's ISRC
                        except Exception as e_isrc_track_search:
                            logger.warning(f"Error during Deezer search for ISRC {isrc_value}: {e_isrc_track_search}")
                            # Continue to the next track's ISRC
                    if not link_dee: # If loop finished and no link found via ISRC
                        logger.warning(f"ISRC method completed for {link_album}, but no matching Deezer album found.")
            except Exception as e_isrc_block:
                logger.error(f"Error during ISRC processing block for {link_album}: {e_isrc_block}")

        if not link_dee:
            raise AlbumNotFound(f"Failed to convert Spotify album link {link_album} to a Deezer link after all attempts.")

        return link_dee

    def download_albumspo(
        self, link_album,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        make_zip=stock_zip,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5,
        convert_to=None
    ) -> Album:

        link_dee = self.convert_spoty_to_dee_link_album(link_album)

        album = self.download_albumdee(
            link_dee, output_dir,
            quality_download, recursive_quality,
            recursive_download, not_interface,
            make_zip, method_save,
            custom_dir_format=custom_dir_format,
            custom_track_format=custom_track_format,
            pad_tracks=pad_tracks,
            initial_retry_delay=initial_retry_delay,
            retry_delay_increase=retry_delay_increase,
            max_retries=max_retries,
            convert_to=convert_to
        )

        return album

    def download_playlistspo(
        self, link_playlist,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        make_zip=stock_zip,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5,
        convert_to=None
    ) -> Playlist:

        link_is_valid(link_playlist)
        ids = get_ids(link_playlist)

        # Use stored credentials for API calls
        playlist_json = Spo.get_playlist(ids)
        playlist_name = playlist_json['name']
        total_tracks = playlist_json['tracks']['total']
        playlist_tracks = playlist_json['tracks']['items']
        playlist = Playlist()
        tracks = playlist.tracks

        # Initializing status - replaced print with report_progress
        self.report_progress({
            "status": "initializing",
            "type": "playlist",
            "name": playlist_name,
            "total_tracks": total_tracks
        })

        for index, item in enumerate(playlist_tracks, 1):
            is_track = item.get('track')
            if not is_track:
                # Progress status for an invalid track item
                self.report_progress({
                    "status": "progress",
                    "type": "playlist",
                    "track": "Unknown Track",
                    "current_track": f"{index}/{total_tracks}"
                })
                continue

            track_info = is_track
            track_name = track_info.get('name', 'Unknown Track')
            artists = track_info.get('artists', [])
            artist_name = artists[0]['name'] if artists else 'Unknown Artist'

            external_urls = track_info.get('external_urls', {})
            if not external_urls:
                # Progress status for unavailable track
                self.report_progress({
                    "status": "progress",
                    "type": "playlist",
                    "track": track_name,
                    "current_track": f"{index}/{total_tracks}"
                })
                logger.warning(f"The track \"{track_name}\" is not available on Spotify :(")
                continue

            # Progress status before download attempt
            self.report_progress({
                "status": "progress",
                "type": "playlist",
                "track": track_name,
                "current_track": f"{index}/{total_tracks}"
            })

            link_track = external_urls['spotify']

            try:
                # Download each track individually via the Spotify-to-Deezer conversion method.
                downloaded_track = self.download_trackspo(
                    link_track,
                    output_dir=output_dir,
                    quality_download=quality_download,
                    recursive_quality=recursive_quality,
                    recursive_download=recursive_download,
                    not_interface=not_interface,
                    method_save=method_save,
                    custom_dir_format=custom_dir_format,
                    custom_track_format=custom_track_format,
                    pad_tracks=pad_tracks,
                    initial_retry_delay=initial_retry_delay,
                    retry_delay_increase=retry_delay_increase,
                    max_retries=max_retries,
                    convert_to=convert_to
                )
                tracks.append(downloaded_track)
            except (TrackNotFound, NoDataApi) as e:
                logger.error(f"Failed to download track: {track_name} - {artist_name}")
                tracks.append(f"{track_name} - {artist_name}")

        # Done status
        self.report_progress({
            "status": "done",
            "type": "playlist",
            "name": playlist_name,
            "total_tracks": total_tracks
        })

        # === New m3u File Creation Section ===
        # Create a subfolder "playlists" inside the output directory
        playlist_m3u_dir = os.path.join(output_dir, "playlists")
        os.makedirs(playlist_m3u_dir, exist_ok=True)
        # The m3u file will be named after the playlist (e.g. "MyPlaylist.m3u")
        m3u_path = os.path.join(playlist_m3u_dir, f"{playlist_name}.m3u")
        with open(m3u_path, "w", encoding="utf-8") as m3u_file:
            # Write the m3u header
            m3u_file.write("#EXTM3U\n")
            # Append each successfully downloaded track's relative path
            for track in tracks:
                if isinstance(track, Track) and track.success and hasattr(track, 'song_path') and track.song_path:
                    # Calculate the relative path from the m3u folder to the track file
                    relative_song_path = os.path.relpath(track.song_path, start=playlist_m3u_dir)
                    m3u_file.write(f"{relative_song_path}\n")
        logger.info(f"Created m3u playlist file at: {m3u_path}")
        # === End m3u File Creation Section ===

        if make_zip:
            playlist_name = playlist_json['name']
            zip_name = f"{output_dir}playlist {playlist_name}.zip"
            create_zip(tracks, zip_name=zip_name)
            playlist.zip_path = zip_name

        return playlist

    def download_name(
        self, artist, song,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        convert_to=None
    ) -> Track:

        query = f"track:{song} artist:{artist}"
        # Use the stored credentials when searching
        search = self.__spo.search(
            query, 
            client_id=self.spotify_client_id, 
            client_secret=self.spotify_client_secret
        ) if not self.__spo._Spo__initialized else self.__spo.search(query)
        
        items = search['tracks']['items']

        if len(items) == 0:
            msg = f"No result for {query} :("
            raise TrackNotFound(message=msg)

        link_track = items[0]['external_urls']['spotify']

        track = self.download_trackspo(
            link_track,
            output_dir=output_dir,
            quality_download=quality_download,
            recursive_quality=recursive_quality,
            recursive_download=recursive_download,
            not_interface=not_interface,
            method_save=method_save,
            custom_dir_format=custom_dir_format,
            custom_track_format=custom_track_format,
            pad_tracks=pad_tracks,
            convert_to=convert_to
        )

        return track

    def download_episode(
        self,
        link_episode,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5,
        convert_to=None
    ) -> Episode:
        
        link_is_valid(link_episode)
        ids = get_ids(link_episode)
        
        try:
            episode_metadata = API.tracking(ids)
        except NoDataApi:
            infos = self.__gw_api.get_episode_data(ids)
            if not infos:
                raise TrackNotFound("Episode not found")
            episode_metadata = {
                'music': infos.get('EPISODE_TITLE', ''),
                'artist': infos.get('SHOW_NAME', ''),
                'album': infos.get('SHOW_NAME', ''),
                'date': infos.get('EPISODE_PUBLISHED_TIMESTAMP', '').split()[0],
                'genre': 'Podcast',
                'explicit': infos.get('SHOW_IS_EXPLICIT', '2'),
                'disc': 1,
                'track': 1,
                'duration': int(infos.get('DURATION', 0)),
                'isrc': None,
                'image': infos.get('EPISODE_IMAGE_MD5', '')
            }

        preferences = Preferences()
        preferences.link = link_episode
        preferences.song_metadata = episode_metadata
        preferences.quality_download = quality_download
        preferences.output_dir = output_dir
        preferences.ids = ids
        preferences.recursive_quality = recursive_quality
        preferences.recursive_download = recursive_download
        preferences.not_interface = not_interface
        preferences.method_save = method_save
        # New custom formatting preferences:
        preferences.custom_dir_format = custom_dir_format
        preferences.custom_track_format = custom_track_format
        # Track number padding option
        preferences.pad_tracks = pad_tracks
        # Retry parameters
        preferences.initial_retry_delay = initial_retry_delay
        preferences.retry_delay_increase = retry_delay_increase
        preferences.max_retries = max_retries

        episode = DW_EPISODE(preferences).dw()

        return episode
    
    def download_smart(
        self, link,
        output_dir=stock_output,
        quality_download=stock_quality,
        recursive_quality=stock_recursive_quality,
        recursive_download=stock_recursive_download,
        not_interface=stock_not_interface,
        make_zip=stock_zip,
        method_save=method_save,
        custom_dir_format=None,
        custom_track_format=None,
        pad_tracks=True,
        initial_retry_delay=30,
        retry_delay_increase=30,
        max_retries=5
    ) -> Smart:

        link_is_valid(link)
        link = what_kind(link)
        smart = Smart()

        if "spotify.com" in link:
            source = "https://spotify.com"
        elif "deezer.com" in link:
            source = "https://deezer.com"

        smart.source = source
        
        # Add progress reporting for the smart downloader
        self.report_progress({
            "status": "initializing",
            "type": "smart_download",
            "link": link,
            "source": source
        })

        if "track/" in link:
            if "spotify.com" in link:
                func = self.download_trackspo
            elif "deezer.com" in link:
                func = self.download_trackdee
            else:
                raise InvalidLink(link)

            track = func(
                link,
                output_dir=output_dir,
                quality_download=quality_download,
                recursive_quality=recursive_quality,
                recursive_download=recursive_download,
                not_interface=not_interface,
                method_save=method_save,
                custom_dir_format=custom_dir_format,
                custom_track_format=custom_track_format,
                pad_tracks=pad_tracks,
                initial_retry_delay=initial_retry_delay,
                retry_delay_increase=retry_delay_increase,
                max_retries=max_retries
            )
            smart.type = "track"
            smart.track = track

        elif "album/" in link:
            if "spotify.com" in link:
                func = self.download_albumspo
            elif "deezer.com" in link:
                func = self.download_albumdee
            else:
                raise InvalidLink(link)

            album = func(
                link,
                output_dir=output_dir,
                quality_download=quality_download,
                recursive_quality=recursive_quality,
                recursive_download=recursive_download,
                not_interface=not_interface,
                make_zip=make_zip,
                method_save=method_save,
                custom_dir_format=custom_dir_format,
                custom_track_format=custom_track_format,
                pad_tracks=pad_tracks,
                initial_retry_delay=initial_retry_delay,
                retry_delay_increase=retry_delay_increase,
                max_retries=max_retries
            )
            smart.type = "album"
            smart.album = album

        elif "playlist/" in link:
            if "spotify.com" in link:
                func = self.download_playlistspo
            elif "deezer.com" in link:
                func = self.download_playlistdee
            else:
                raise InvalidLink(link)

            playlist = func(
                link,
                output_dir=output_dir,
                quality_download=quality_download,
                recursive_quality=recursive_quality,
                recursive_download=recursive_download,
                not_interface=not_interface,
                make_zip=make_zip,
                method_save=method_save,
                custom_dir_format=custom_dir_format,
                custom_track_format=custom_track_format,
                pad_tracks=pad_tracks,
                initial_retry_delay=initial_retry_delay,
                retry_delay_increase=retry_delay_increase,
                max_retries=max_retries
            )
            smart.type = "playlist"
            smart.playlist = playlist
            
        # Report completion
        self.report_progress({
            "status": "done",
            "type": "smart_download",
            "source": source,
            "content_type": smart.type
        })

        return smart
