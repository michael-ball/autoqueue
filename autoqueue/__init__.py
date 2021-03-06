"""
AutoQueue: an automatic queueing plugin library.

Copyright 2007-2016 Eric Casteleijn <thisfred@gmail.com>,
                    Daniel Nouri <daniel.nouri@gmail.com>
                    Jasper OpdeCoul <jasper.opdecoul@gmail.com>
                    Graham White
                    Naglis Jonaitis <njonaitis@gmail.com>

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2, or (at your option)
any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA.
"""
from __future__ import absolute_import, division, print_function

import random
import re
from collections import Counter
from datetime import datetime, timedelta
from builtins import object, range, str

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from future import standard_library

try:
    import pyowm
    WEATHER = True
except ImportError:
    WEATHER = False
try:
    import geohash
    GEOHASH = True
except ImportError:
    GEOHASH = False
import requests
from autoqueue.blocking import Blocking
from autoqueue.context import Context, get_terms_from_song
from autoqueue.request import Requests


standard_library.install_aliases()

DBusGMainLoop(set_as_default=True)

# If you change even a single character of code, I would ask that you
# get and use your own (free) last.fm api key from here:
# http://www.last.fm/api/account
API_KEY = "09d0975a99a4cab235b731d31abf0057"
THRESHOLD = .5
TIMEOUT = 3000
FIVE_MINUTES = timedelta(minutes=5)
DEFAULT_NUMBER = 20
DEFAULT_LENGTH = 15 * 60
BANNED_ALBUMS = [
    'ep', 'greatest hits', 'the greatest hits', 'demo', 'the best of',
    'the very best of', 'live', 'demos', 'self titled', 'untitled album',
    '[non-album tracks]', 'single', 'singles', '7"', 'covers', 'album',
    'split 7"', 's/t']


def no_op(*args, **kwargs):
    pass


def tag_score(song, tags):
    """Calculate similarity score by tags."""
    if not tags:
        return 0
    song_tags = set(song.get_non_geo_tags())
    if not song_tags:
        return 0
    return len(song_tags & tags) / min(len(song_tags), len(tags))


class Configuration(object):

    def __init__(self):
        self.contextualize = True
        self.desired_queue_length = DEFAULT_LENGTH
        self.number = DEFAULT_NUMBER
        self.restrictions = None
        self.extra_context = None
        self.whole_albums = True
        self.southern_hemisphere = False
        self.favor_new = True
        self.use_lastfm = True
        self.use_groupings = True
        self.location = ''
        self.geohash = ''
        self.birthdays = ''
        self.use_gaia = True
        self.zipcode = ''

    def get_weather(self):
        if WEATHER and self.location:
            return self._get_weather(self.location)

        return {}

    def get_performing_artists(self):
        """Get a list of found playing nearby venues in the near future."""
        nearby_artists = []
        for page in self._get_pages():
            nearby_artists.extend(self._get_artists(page))
        return nearby_artists

    def _get_pages(self):
        parameters = self._build_parameters()
        while True:
            page = self._get_page(parameters)
            if 'events' not in page:
                print(page)
                return
            total_pages = int(page['events']['@attr']['totalPages'])
            page_number = int(page['events']['@attr']['page'])
            yield page
            if page_number == total_pages:
                return
            parameters['page'] = page_number + 1
            page = self._get_page(parameters)

    @staticmethod
    def _get_artists(page):
        artists = []
        for event in page['events']['event']:
            if not isinstance(event, dict):
                continue
            found = event['artists']['artist']
            if not isinstance(found, list):
                found = [found]
            artists.extend(found)
        return artists

    @staticmethod
    def _get_page(parameters):
        response = None
        try:
            response = requests.get(
                'http://ws.audioscrobbler.com/2.0/', params=parameters)
            page = response.json()
        except Exception as ex:
            print(ex)
            print(response)
            return {}
        return page

    def _build_parameters(self):
        parameters = {
            'method': 'geo.getevents',
            'limit': 25,
            'api_key': API_KEY,
            'format': 'json'}
        if self.geohash and GEOHASH:
            lon, lat = geohash.decode(self.geohash)[:2]
            parameters['long'] = lon
            parameters['lat'] = lat
        if self.location:
            parameters['location'] = self.location
        return parameters

    @staticmethod
    def _get_weather(location):
        try:
            # If you use this code for anything else, please register for your
            # own OWM API key for free, here:
            #
            # https://home.openweathermap.org/users/sign_up
            owm = pyowm.OWM("35c8c197224e0fb5f7a771facb4243ae")
            return owm.weather_at_place(location).get_weather()
        except Exception as exception:
            print(repr(exception))
        return {}


class Cache(object):

    def __init__(self):
        self.song = None
        self.running = False
        self.last_songs = []
        self.last_song = None
        self.nearby_artists = []
        self.weather = None
        self.weather_at = None
        self.found = False
        self.previous_terms = Counter()

    def add_to_previous_terms(self, song):
        self.previous_terms -= Counter(self.previous_terms.keys())
        terms = get_terms_from_song(song)
        for _ in range(2):
            self.previous_terms.update(terms)

    def get_weather(self, configuration):
        if not WEATHER:
            return None

        if self.weather and datetime.now() < self.weather_at + FIVE_MINUTES:
            return self.weather
        self.weather = configuration.get_weather()
        self.weather_at = datetime.now()
        return self.weather

    def set_nearby_artist(self, configuration):
        if configuration.location or configuration.geohash:
            self.nearby_artists = configuration.get_performing_artists()


class AutoQueueBase(object):

    """Generic base class for autoqueue plugins."""

    def __init__(self, player):
        self._cache_dir = None
        self.blocking = Blocking()
        self.configuration = Configuration()
        self.context = None
        self.cache = Cache()
        bus = dbus.SessionBus()
        sim = bus.get_object(
            'org.autoqueue', '/org/autoqueue/Similarity',
            follow_name_owner_changes=True)
        self.similarity = dbus.Interface(
            sim, dbus_interface='org.autoqueue.SimilarityInterface')
        self.has_gaia = self.similarity.has_gaia()
        self.player = player
        self.player.set_variables_from_config(self.configuration)
        self.cache.set_nearby_artist(self.configuration)
        self.requests = Requests()
        self.current_request = None

    @property
    def use_gaia(self):
        return self.configuration.use_gaia and self.has_gaia

    @staticmethod
    def error_handler(*args, **kwargs):
        """Log errors when calling D-Bus methods in a async way."""
        print('Error handler received: %r, %r' % (args, kwargs))

    def allowed(self, song):
        """Check whether a song is allowed to be queued."""
        filename = song.get_filename()
        for qsong in self.get_last_songs():
            if qsong.get_filename() == filename:
                return False

        if self.requests.has(filename):
            return True

        date_search = re.compile("([0-9]{4}-)?%02d-%02d" % (
            self.eoq.month, self.eoq.day))
        for tag in song.get_stripped_tags():
            if date_search.match(tag):
                return True

        for artist in song.get_artists():
            if artist in self.blocking.get_blocked_artists(
                    self.get_last_songs()):
                return False

        return True

    def on_song_ended(self, song, skipped):
        """Should be called by the plugin when a song ends or is skipped."""
        if song is None:
            return

        if skipped:
            return

        for artist_name in song.get_artists():
            self.blocking.block_artist(artist_name)

    def on_song_started(self, song):
        """Should be called by the plugin when a new song starts.

        If the right conditions apply, we start looking for new songs
        to queue.

        """
        if song is None:
            return
        self.cache.song = song
        if self.cache.running:
            return
        if self.configuration.desired_queue_length == 0 or \
                self.queue_needs_songs():
            self.queue_song()
        self.blocking.unblock_artists()
        self.pop_request(song)

    def pop_request(self, song):
        self.requests.pop(song.get_filename())

    def on_removed(self, songs):
        if not self.use_gaia:
            return
        for song in songs:
            self.remove_missing_track(song.get_filename())
            self.pop_request(song)

    def queue_needs_songs(self):
        """Determine whether the queue needs more songs added."""
        queue_length = self.player.get_queue_length()
        return queue_length < self.configuration.desired_queue_length

    @property
    def eoq(self):
        return datetime.now() + timedelta(0, self.player.get_queue_length())

    def construct_filenames_search(self, filenames):
        return self.player.construct_files_search(filenames)

    def construct_search(self, artist=None, title=None, tags=None,
                         filename=None):
        """Construct a search based on several criteria."""
        if filename:
            return self.player.construct_file_search(filename)
        if title:
            return self.player.construct_track_search(artist, title)
        if artist:
            return self.player.construct_artist_search(artist)
        if tags:
            return self.player.construct_tag_search(tags)

    def queue_song(self):
        """Queue a single track."""
        self.cache.running = True
        self.cache.last_songs = self.get_last_songs()
        song = self.cache.last_song = self.cache.last_songs.pop()
        self.analyze_and_callback(
            song.get_filename(), reply_handler=self.analyzed,
            empty_handler=self.gaia_reply_handler)

    def get_best_request(self, filename):
        all_requests = self.requests.get_requests()
        if not all_requests:
            self.similarity.get_ordered_gaia_tracks(
                filename, self.configuration.number,
                reply_handler=self.gaia_reply_handler,
                error_handler=self.error_handler, timeout=TIMEOUT)
        elif len(all_requests) == 1:
            self.best_request_handler(all_requests[0])
        else:
            self.similarity.get_best_match(
                filename, all_requests,
                reply_handler=self.best_request_handler,
                error_handler=self.error_handler, timeout=TIMEOUT)

    def best_request_handler(self, request):
        song = self.cache.last_song
        filename = ensure_string(song.get_filename())
        if not filename:
            return

        if request:
            print("*****" + request)
            self.current_request = ensure_string(request)
            if request:
                self.similarity.get_ordered_gaia_tracks_by_request(
                    filename, self.configuration.number, self.current_request,
                    reply_handler=self.gaia_reply_handler,
                    error_handler=self.error_handler, timeout=TIMEOUT)
                return

        else:
            print("***** no requests")
        self.similarity.get_ordered_gaia_tracks(
            filename, self.configuration.number,
            reply_handler=self.gaia_reply_handler,
            error_handler=self.error_handler, timeout=TIMEOUT)

    def analyzed(self):
        song = self.cache.last_song
        filename = ensure_string(song.get_filename())
        if not filename:
            return
        if self.use_gaia:
            print('Get similar tracks for: %s' % filename)
            self.get_best_request(filename)
        else:
            self.gaia_reply_handler([])

    def gaia_reply_handler(self, results):
        """Handler for (gaia) similar tracks returned from dbus."""
        self.player.execute_async(self._gaia_reply_handler, results=results)

    def continue_queueing(self):
        if not self.queue_needs_songs():
            self.done()
        else:
            self.queue_song()

    def _gaia_reply_handler(self, results=None):
        """Exexute processing asynchronous."""
        self.cache.found = False
        if results:
            for _ in self.process_filename_results([
                    {'score': match, 'filename': filename}
                    for match, filename in results]):
                yield

        if self.cache.found:
            self.continue_queueing()
            return

        if self.current_request:
            song = self.cache.last_song
            filename = ensure_string(song.get_filename())
            if not filename:
                return
            self.current_request = None
            self.similarity.get_ordered_gaia_tracks(
                filename, self.configuration.number,
                reply_handler=self.gaia_reply_handler,
                error_handler=self.error_handler, timeout=TIMEOUT)
            return
        self.get_similar_tracks()

    def get_similar_tracks(self):
        if not self.configuration.use_lastfm:
            self.similar_artists_handler([])
            return

        last_song = self.cache.last_song
        artist_name = last_song.get_artist()
        title = last_song.get_title()
        if artist_name and title:
            print('Get similar tracks for: %s - %s' % (artist_name, title))
            self.similarity.get_ordered_similar_tracks(
                artist_name, title,
                reply_handler=self.similar_tracks_handler,
                error_handler=self.error_handler, timeout=TIMEOUT)
        else:
            self.similar_tracks_handler([])

    def done(self):
        """Analyze the last song and stop."""
        song = self.get_last_songs()[-1]
        self.analyze_and_callback(song.get_filename())
        self.cache.running = False

    def similar_tracks_handler(self, results):
        """Handler for similar tracks returned from dbus."""
        self.player.execute_async(
            self._similar_tracks_handler, results=results)

    def _similar_tracks_handler(self, results=None):
        """Exexute processing asynchronous."""
        self.cache.found = False
        for _ in self.process_results([{'score': match, 'artist': artist,
                                        'title': title} for match, artist,
                                       title in results], invert_scores=True):
            yield
        if self.cache.found:
            self.continue_queueing()
            return
        self.get_similar_artists()

    def get_similar_artists(self):
        artists = [
            a.encode('utf-8') for a in self.cache.last_song.get_artists()]
        print('Get similar artists for %s' % artists)
        self.similarity.get_ordered_similar_artists(
            artists, reply_handler=self.similar_artists_handler,
            error_handler=self.error_handler, timeout=TIMEOUT)

    def similar_artists_handler(self, results):
        """Handler for similar artists returned from dbus."""
        self.player.execute_async(
            self._similar_artists_handler, results=results)

    def _similar_artists_handler(self, results=None):
        """Exexute processing asynchronous."""
        self.cache.found = False
        if results:
            for _ in self.process_results([{'score': match, 'artist': artist}
                                           for match, artist in results],
                                          invert_scores=True):
                yield

        if self.cache.found:
            self.continue_queueing()
            return

        if self.configuration.use_groupings:
            for _ in self.process_results(
                    self.get_ordered_similar_by_tag(self.cache.last_song),
                    invert_scores=True):
                yield

            if self.cache.found:
                self.continue_queueing()
                return

        if not self.cache.last_songs:
            self.cache.running = False
            return
        song = self.cache.last_song = self.cache.last_songs.pop()
        self.analyze_and_callback(
            song.get_filename(), reply_handler=self.analyzed,
            empty_handler=self.gaia_reply_handler)

    def analyze_and_callback(self, filename, reply_handler=no_op,
                             empty_handler=no_op):
        filename = ensure_string(filename)
        if not filename:
            return
        if self.use_gaia:
            print('Analyzing: %s' % filename)
            self.similarity.analyze_track(
                filename, reply_handler=reply_handler,
                error_handler=self.error_handler, timeout=TIMEOUT)
        else:
            empty_handler([])

    @staticmethod
    def satisfies(song, criteria):
        """Check whether the song satisfies any of the criteria."""
        filename = criteria.get('filename')
        if filename:
            return filename == song.get_filename()
        title = criteria.get('title')
        artist = criteria.get('artist')
        if title:
            return (
                song.get_title().lower() == title.lower() and
                song.get_artist().lower() == artist.lower())
        if artist:
            artist_lower = artist.lower()
            for song_artist in song.get_artists():
                if song_artist.lower() == artist_lower:
                    return True
            return False
        tags = criteria.get('tags')
        song_tags = song.get_tags()
        for tag in tags:
            if (tag in song_tags or 'artist:%s' % (tag,) in song_tags or
                    'album:%s' % (tag,) in song_tags):
                return True
        return False

    def search_filenames(self, results):
        filenames = [r['filename'] for r in results]
        search = self.construct_filenames_search(filenames)
        self.perform_search(search, results)

    def search_database(self, results):
        """Search for songs in results."""
        for result in results:
            search = self.construct_search(
                artist=result.get('artist'), title=result.get('title'),
                filename=result.get('filename'), tags=result.get('tags'))
            self.perform_search(search, [result])
            yield

    def get_current_request(self):
        filename = self.current_request
        if not filename:
            return

        search = self.construct_filenames_search([filename])
        songs = self.player.search(search)
        if not songs:
            return

        return songs[0]

    def perform_search(self, search, results):
        songs = set(
            self.player.search(
                search, restrictions=self.configuration.restrictions))
        found = set()
        for result in results:
            for song in songs - found:
                if self.satisfies(song, result):
                    result['song'] = song
                    found.add(song)
                    break
            else:
                if not self.configuration.restrictions:
                    filename = result.get('filename')
                    if filename:
                        self.remove_missing_track(filename)

    def remove_missing_track(self, filename):
        print('Remove similarity for %s' % filename)
        if not isinstance(filename, str):
            filename = str(filename, 'utf-8')
        self.similarity.remove_track_by_filename(
            filename, reply_handler=no_op,
            error_handler=self.error_handler, timeout=TIMEOUT)

    def adjust_scores(self, results, invert_scores):
        """Adjust scores based on similarity with previous song and context."""
        if self.configuration.contextualize:
            self.context = Context(
                context_date=self.eoq, configuration=self.configuration,
                cache=self.cache, request=self.get_current_request())
            maximum_score = max(result['score'] for result in results) + 1
            for result in results[:]:
                if 'song' not in result:
                    results.remove(result)
                    continue
                if invert_scores:
                    result['score'] = maximum_score - result['score']
                self.context.adjust_score(result)
                yield

    def process_results(self, results, invert_scores=False):
        """Process results and queue best one(s)."""
        if not results:
            return
        for _ in self.search_database(results):
            yield
        for _ in self.adjust_scores(results, invert_scores):
            yield
        if not results:
            return
        self.pick_result(results)

    def pick_result(self, results):
        number_of_results = len(results)
        for number, result in enumerate(sorted(results,
                                               key=lambda x: x['score'])):
            song = result.get('song')
            if not song:
                print("'song' not found in %s" % result)
                continue
            self.log_lookup(number, result)
            frequency = song.get_play_frequency()
            if frequency is NotImplemented:
                frequency = 1
            rating = song.get_rating()
            if rating is NotImplemented:
                rating = THRESHOLD
            for reason in result.get('reasons', []):
                print("  %s" % (reason,))

            if number_of_results > 1:
                print("score: %.5f, play frequency %.5f" % (rating, frequency))
                comparison = rating
                if self.configuration.favor_new:
                    comparison -= frequency
                if (frequency > 0 or not self.configuration.favor_new) and \
                        random.random() > comparison:
                    continue

            if not self.allowed(song):
                continue

            if self.maybe_enqueue_album(song):
                self.cache.found = True
                return

            if self.allowed(song):
                self.enqueue_song(song)
                self.cache.found = True
                return

    def process_filename_results(self, results):
        if not results:
            return
        self.search_filenames(results)
        for _ in self.adjust_scores(results, invert_scores=False):
            yield
        if not results:
            return
        self.pick_result(results)

    @staticmethod
    def log_lookup(number, result):
        look_for = str(result.get('artist', ''))
        if look_for:
            title = str(result.get('title', ''))
            if title:
                look_for += ' - ' + title
        elif 'filename' in result:
            look_for = str(result['filename'])
        elif 'tags' in result:
            look_for = result['tags']
        else:
            print(repr(result))
            look_for = str(result)
        print('%03d: %06d %s' % (number + 1, result.get('score', 0), look_for))

    def maybe_enqueue_album(self, song):
        """Determine if a whole album should be queued, and do so."""
        if (self.configuration.whole_albums and song.get_tracknumber() == 1 and
                (song.get_playcount() == 0 or random.random() > .5)):
            album = song.get_album()
            album_artist = song.get_album_artist()
            album_id = song.get_musicbrainz_albumid()
            if album and album.lower() not in BANNED_ALBUMS:
                return self.enqueue_album(album, album_artist, album_id)

        return False

    def enqueue_song(self, song):
        self.cache.add_to_previous_terms(song)
        self.player.enqueue(song)

    def enqueue_album(self, album, album_artist, album_id):
        """Try to enqueue whole album."""
        search = self.player.construct_album_search(
            album=album, album_artist=album_artist, album_id=album_id)
        songs = sorted([
            (song.get_discnumber(), song.get_tracknumber(), song)
            for song in self.player.search(search)])
        if songs:
            for _, _, song in songs:
                self.enqueue_song(song)
            return True
        return False

    def get_last_songs(self):
        """Return the currently playing song plus the songs in the queue."""
        queue = self.player.get_songs_in_queue() or []
        return [self.cache.song] + queue

    def get_ordered_similar_by_tag(self, last_song):
        """Get similar tracks by tag."""
        tag_set = set(last_song.get_non_geo_tags())
        if not tag_set:
            return []
        search = self.construct_search(tags=list(tag_set))
        songs = sorted(
            [(tag_score(song, tag_set), song)
             for song in self.player.search(search)],
            reverse=True)
        return [
            {'score': score, 'filename': song.get_filename()}
            for score, song in songs]


def levenshtein(string1, string2):
    """Calculate the Levenshtein distance between two strings."""
    if len(string1) < len(string2):
        return levenshtein(string2, string1)

    if not string2:
        return len(string1)

    previous_row = list(range(len(string2) + 1))
    for i, character1 in enumerate(string1):
        current_row = [i + 1]
        for j, character2 in enumerate(string2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (character1 != character2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def ensure_string(possible_string):
    """Convert unicode to utf-8 string, or return string or return None."""
    try:
        if isinstance(possible_string, unicode):
            return possible_string.encode('utf-8')
    except UnicodeEncodeError:
        print('Could not encode filename: %r' % possible_string)
        return None

    return possible_string
