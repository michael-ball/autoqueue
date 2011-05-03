"""Autoqueue similarity service."""

import dbus
import dbus.service
import gobject
import os
import urllib

from threading import Thread
from Queue import Queue, PriorityQueue
from time import strptime, sleep
from datetime import datetime, timedelta

from xml.dom import minidom

from dbus.mainloop.glib import DBusGMainLoop
from dbus.service import method

import sqlite3

from mirage import (
    Mir, MatrixDimensionMismatchException, MfccFailedException,
    instance_from_picklestring, instance_to_picklestring, ScmsConfiguration,
    distance)

try:
    import xdg.BaseDirectory
    XDG = True
except ImportError:
    XDG = False

DBusGMainLoop(set_as_default=True)

DBUS_BUSNAME = 'org.autoqueue'
DBUS_IFACE = 'org.autoqueue.SimilarityInterface'
DBUS_PATH = '/org/autoqueue/Similarity'

# If you change even a single character of code, I would ask that you
# get and use your own (free) last.fm api key from here:
# http://www.last.fm/api/account
API_KEY = "09d0975a99a4cab235b731d31abf0057"

TRACK_URL = "http://ws.audioscrobbler.com/2.0/?method=track.getsimilar" \
            "&artist=%s&track=%s&api_key=" + API_KEY
ARTIST_URL = "http://ws.audioscrobbler.com/2.0/?method=artist.getsimilar" \
             "&artist=%s&api_key=" + API_KEY

# be nice to last.fm
WAIT_BETWEEN_REQUESTS = timedelta(0, 1)

# TODO make configurable
NEIGHBOURS = 20


class Throttle(object):
    """Decorator that throttles calls to a function or method."""

    def __init__(self, wait):
        self.wait = wait
        self.last_called = datetime.now()

    def __call__(self, func):
        """Return the decorator."""

        def wrapper(*args, **kwargs):
            """The implementation of the decorator."""
            while self.last_called + self.wait > datetime.now():
                sleep(0.1)
            result = func(*args, **kwargs)
            self.last_called = datetime.now()
            return result

        return wrapper


class SQLCommand(object):
    """A SQL command object."""

    def __init__(self, sql_statements):
        self.sql = sql_statements
        self.result_queue = Queue()


class DatabaseWrapper(Thread):
    """Process to handle all database access."""

    def set_path(self, path):
        """Set the database path."""
        self.path = path

    def set_queue(self, queue):
        """Set the queue to use."""
        self.queue = queue

    def run(self):
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level="immediate")
        cursor = connection.cursor()
        while True:
            priority, cmd = self.queue.get()
            sql = cmd.sql
            if sql == ('STOP',):
                cmd.result_queue.put(None)
                connection.close()
                break
            commit_needed = False
            result = []
            try:
                cursor.execute(*sql)
            except:
                for s in sql:
                    print repr(s)
            if not sql[0].upper().startswith('SELECT'):
                commit_needed = True
            for row in cursor.fetchall():
                result.append(row)
            if commit_needed:
                connection.commit()
            cmd.result_queue.put(result)


class Db(object):
    """Database access class."""

    def __init__(self):
        self._data_dir = None
        self.db_path = os.path.join(
            self.player_get_data_dir(), "similarity.db")
        self.queue = PriorityQueue()
        self._db_wrapper = DatabaseWrapper()
        self._db_wrapper.daemon = True
        self._db_wrapper.set_path(self.db_path)
        self._db_wrapper.set_queue(self.queue)
        self._db_wrapper.start()
        self.create_db()
        print "Db created."

    def execute_sql(self, sql, priority=1):
        """Put sql command on the queue to be executed."""
        command = SQLCommand(sql)
        self.queue.put((priority, command))
        return command.result_queue.get()

    def player_get_data_dir(self):
        """Get the directory to store user data.

        Defaults to $XDG_DATA_HOME/autoqueue on Gnome.

        """
        if self._data_dir:
            return self._data_dir
        if not XDG:
            return NotImplemented
        data_dir = os.path.join(xdg.BaseDirectory.xdg_data_home, 'autoqueue')
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        self._data_dir = data_dir
        return data_dir

    def add_track(self, filename, scms, priority):
        """Add track to database."""
        self.execute_sql(
            ("INSERT INTO mirage (filename, scms) VALUES (?, ?);",
            (filename, sqlite3.Binary(instance_to_picklestring(scms)))),
            priority=priority)

    def remove_track_by_filename(self, filename):
        """Remove tracks from database."""
        for row in self.execute_sql(
            ('SELECT trackid FROM mirage WHERE filename = ?', (filename,)),
            priority=10):
            track_id = row[0]
            self.execute_sql((
                'DELETE FROM distance WHERE track_1 = ? OR track_2 = ?;',
            (track_id, track_id)), priority=10)
            self.execute_sql((
                "DELETE FROM mirage WHERE trackid = ?;",
                (track_id,)), priority=10)

    def remove_track(self, artist, title):
        """Delete missing track."""
        for row in self.execute_sql((
            'SELECT tracks.id FROM tracks WHERE tracks.title = ? AND WHERE '
            'tracks.artist IN (SELECT artists.id FROM artists WHERE '
            'artists.name = ?);', (artist, title)), priority=10):
            track_id = row[0]
            self.execute_sql(
                ('DELETE FROM track_2_track WHERE track1 = ? or track2 = ?;',
                 (track_id, track_id)), priority=10)
            self.execute_sql(
                ('DELETE FROM tracks WHERE id = ?;', (track_id,)), priority=10)
        self.delete_orphan_artist(artist)

    def remove_artist(self, artist):
        """Delete missing artist."""
        for row in self.execute_sql(
            ('SELECT id from artists WHERE artists.name = ?;', (artist,)),
            priority=10):
            artist_id = row[0]
            self.execute_sql(
                ('DELETE FROM artists WHERE artists.id = ?;', (artist_id)),
                priority=10)
            self.execute_sql(
                ('DELETE FROM tracks WHERE tracks.artist = ?;', (artist_id)),
                priority=10)

    def get_track_from_filename(self, filename, priority):
        """Get track from database."""
        rows = self.execute_sql((
            "SELECT trackid, scms FROM mirage WHERE filename = ?;",
            (filename,)), priority=priority)
        for row in rows:
            return (row[0], instance_from_picklestring(row[1]))
        return None

    def get_track_id(self, filename, priority):
        """Get track id from database."""
        rows = self.execute_sql(
            ("SELECT trackid FROM mirage WHERE filename = ?;", (filename,)),
            priority=priority)
        for row in rows:
            return row[0]
        return None

    def has_scores(self, trackid, no=20, priority=0):
        """Check if the track has sufficient neighbours."""
        rows = self.execute_sql((
            'SELECT COUNT(*) FROM distance WHERE track_1 = ?;',
            (trackid,)), priority=priority)
        l1 = rows[0][0]
        if l1 < no:
            print "Only %d connections found, minimum %d." % (l1, no)
            return False
        rows = self.execute_sql((
            "SELECT COUNT(track_1) FROM distance WHERE track_2 = ? AND "
            "distance < (SELECT MAX(distance) FROM distance WHERE track_1 = "
            "?);", (trackid, trackid)), priority=priority)
        l2 = rows[0][0]
        if l2 > l1:
            print "Found %d incoming connections and only %d outgoing." % (
                l2, l1)
            return False
        return True

    def get_tracks(self, exclude_filenames=None, priority=0):
        """Get tracks from database."""
        if not exclude_filenames:
            exclude_filenames = []
        rows = self.execute_sql((
            "SELECT scms, trackid, filename FROM mirage;",), priority=priority)
        return [(row[0], row[1]) for row in rows if row[2]
                  not in exclude_filenames]

    def add_neighbours(self, trackid, scms, exclude_filenames=None,
                       neighbours=20, priority=0):
        """Add best similarity scores to db."""
        if not exclude_filenames:
            exclude_filenames = []
        to_add = neighbours * 2
        _ = self.execute_sql(
            ("DELETE FROM distance WHERE track_1 = ?;", (trackid,)),
            priority=priority)
        conf = ScmsConfiguration(20)
        best = []
        for buf, otherid in self.get_tracks(
                exclude_filenames=exclude_filenames, priority=priority):
            if trackid == otherid:
                continue
            other = instance_from_picklestring(buf)
            dist = int(distance(scms, other, conf) * 1000)
            if dist < 0:
                continue
            if len(best) > to_add - 1:
                if dist > best[-1][0]:
                    continue
            best.append((dist, trackid, otherid))
            best.sort()
            while len(best) > to_add:
                best.pop()
        added = 0
        if best:
            while best:
                added += 1
                best_tup = best.pop()
                self.execute_sql((
                    "INSERT INTO distance (distance, track_1, track_2) "
                    "VALUES (?, ?, ?);", best_tup), priority=priority)
        print "added %d connections" % added

    def get_neighbours(self, trackid):
        """Get neighbours for track."""
        return self.execute_sql((
            "SELECT distance, filename FROM distance INNER JOIN MIRAGE ON "
            "distance.track_2 = mirage.trackid WHERE track_1 = ? ORDER BY "
            "distance ASC;",
            (trackid,)), priority=0)

    def get_artist(self, artist_name):
        """Get artist information from the database."""
        artist_name = artist_name.encode("UTF-8")
        rows = self.execute_sql((
            "SELECT * FROM artists WHERE name = ?;", (artist_name,)))
        for row in rows:
            return row
        _ = self.execute_sql((
            "INSERT INTO artists (name) VALUES (?);", (artist_name,)))
        rows = self.execute_sql((
            "SELECT * FROM artists WHERE name = ?;", (artist_name,)))
        for row in rows:
            return row

    def get_track_from_artist_and_title(self, artist_name, title):
        """Get track information from the database."""
        title = title.encode("UTF-8")
        artist_id = self.get_artist(artist_name)[0]
        rows = self.execute_sql((
            "SELECT * FROM tracks WHERE artist = ? AND title = ?;",
            (artist_id, title)), priority=0)
        for row in rows:
            return row
        _ = self.execute_sql((
            "INSERT INTO tracks (artist, title) VALUES (?, ?);",
            (artist_id, title)), priority=0)
        rows = self.execute_sql((
            "SELECT * FROM tracks WHERE artist = ? AND title = ?;",
            (artist_id, title)), priority=0)
        for row in rows:
            return row

    def get_similar_tracks(self, track_id):
        """Get similar tracks from the database.

        Sorted by descending match score.

        """
        return self.execute_sql((
            "SELECT track_2_track.match, artists.name, tracks.title"
            " FROM track_2_track INNER JOIN tracks ON"
            " track_2_track.track2 = tracks.id INNER JOIN artists ON"
            " artists.id = tracks.artist WHERE track_2_track.track1"
            " = ? ORDER BY track_2_track.match DESC;",
            (track_id,)), priority=0)

    def get_similar_artists(self, artist_id):
        """Get similar artists from the database.

        Sorted by descending match score.

        """
        return self.execute_sql((
            "SELECT match, name FROM artist_2_artist INNER JOIN"
            " artists ON artist_2_artist.artist2 = artists.id WHERE"
            " artist_2_artist.artist1 = ? ORDER BY match DESC;",
            (artist_id,)), priority=0)

    def get_artist_match(self, artist1, artist2):
        """Get artist match score from database."""
        rows = self.execute_sql((
            "SELECT match FROM artist_2_artist WHERE artist1 = ?"
            " AND artist2 = ?;",
            (artist1, artist2)), priority=2)
        for row in rows:
            return row[0]
        return 0

    def get_track_match(self, track1, track2):
        """Get track match score from database."""
        rows = self.execute_sql((
            "SELECT match FROM track_2_track WHERE track1 = ? AND track2 = ?;",
            (track1, track2)), priority=2)
        for row in rows:
            return row[0]
        return 0

    def update_artist_match(self, artist1, artist2, match):
        """Write match score to the database."""
        self.execute_sql((
            "UPDATE artist_2_artist SET match = ? WHERE artist1 = ? AND"
            " artist2 = ?;",
            (match, artist1, artist2)), priority=10)

    def update_track_match(self, track1, track2, match):
        """Write match score to the database."""
        self.execute_sql((
            "UPDATE track_2_track SET match = ? WHERE track1 = ? AND"
            " track2 = ?;",
            (match, track1, track2)), priority=10)

    def insert_artist_match(self, artist1, artist2, match):
        """Write match score to the database."""
        self.execute_sql((
            "INSERT INTO artist_2_artist (artist1, artist2, match) VALUES"
            " (?, ?, ?);",
            (artist1, artist2, match)), priority=10)

    def insert_track_match(self, track1, track2, match):
        """Write match score to the database."""
        self.execute_sql((
            "INSERT INTO track_2_track (track1, track2, match) VALUES"
            " (?, ?, ?);",
            (track1, track2, match)), priority=10)

    def update_artist(self, artist_id):
        """Write artist information to the database."""
        self.execute_sql((
            "UPDATE artists SET updated = DATETIME('now') WHERE id = ?;",
            (artist_id,)), priority=10)

    def update_track(self, track_id):
        """Write track information to the database."""
        self.execute_sql((
            "UPDATE tracks SET updated = DATETIME('now') WHERE id = ?",
            (track_id,)), priority=10)

    def update_similar_artists(self, artists_to_update):
        """Write similar artist information to the database."""
        for artist_id, similar in artists_to_update.items():
            for artist in similar:
                id2 = self.get_artist(artist['artist'])[0]
                if self.get_artist_match(artist_id, id2):
                    self.update_artist_match(artist_id, id2, artist['score'])
                    continue
                self.insert_artist_match(artist_id, id2, artist['score'])
            self.update_artist(artist_id)

    def update_similar_tracks(self, tracks_to_update):
        """Write similar track information to the database."""
        for track_id, similar in tracks_to_update.items():
            for track in similar:
                id2 = self.get_track_from_artist_and_title(
                    track['artist'], track['title'])[0]
                if self.get_track_match(track_id, id2):
                    self.update_track_match(track_id, id2, track['score'])
                    continue
                self.insert_track_match(track_id, id2, track['score'])
            self.update_track(track_id)

    def create_db(self):
        """Set up a database for the artist and track similarity scores."""
        self.execute_sql((
            'CREATE TABLE IF NOT EXISTS artists (id INTEGER PRIMARY KEY, name'
            ' VARCHAR(100), updated DATE);',), priority=0)
        self.execute_sql((
            'CREATE TABLE IF NOT EXISTS artist_2_artist (artist1 INTEGER,'
            ' artist2 INTEGER, match INTEGER);',), priority=0)
        self.execute_sql((
            'CREATE TABLE IF NOT EXISTS tracks (id INTEGER PRIMARY KEY, artist'
            ' INTEGER, title VARCHAR(100), updated DATE);',), priority=0)
        self.execute_sql((
            'CREATE TABLE IF NOT EXISTS track_2_track (track1 INTEGER, track2'
            ' INTEGER, match INTEGER);',), priority=0)
        self.execute_sql((
            'CREATE TABLE IF NOT EXISTS mirage (trackid INTEGER PRIMARY KEY, '
            'filename VARCHAR(300), scms BLOB);',), priority=0)
        self.execute_sql((
            "CREATE TABLE IF NOT EXISTS distance (track_1 INTEGER, track_2 "
            "INTEGER, distance INTEGER);",), priority=0)
        self.execute_sql((
            "CREATE INDEX IF NOT EXISTS a2aa1x ON artist_2_artist "
            "(artist1);",), priority=0)
        self.execute_sql((
            "CREATE INDEX IF NOT EXISTS a2aa2x ON artist_2_artist "
            "(artist2);",), priority=0)
        self.execute_sql(
            ("CREATE INDEX IF NOT EXISTS t2tt1x ON track_2_track (track1);",),
            priority=0)
        self.execute_sql(
            ("CREATE INDEX IF NOT EXISTS t2tt2x ON track_2_track (track2);",),
            priority=0)
        self.execute_sql(
            ("CREATE INDEX IF NOT EXISTS mfnx ON mirage (filename);",),
            priority=0)
        self.execute_sql(
            ("CREATE INDEX IF NOT EXISTS dtrack1x ON distance (track_1);",),
            priority=0)
        self.execute_sql(
            ("CREATE INDEX IF NOT EXISTS dtrack2x ON distance (track_2);",),
            priority=0)

    def delete_orphan_artist(self, artist):
        """Delete artists that have no tracks."""
        for row in self.execute_sql((
                'SELECT artists.id FROM artists WHERE artists.name = ? AND '
                'artists.id NOT IN (SELECT tracks.artist from tracks);',
                (artist,)), priority=10):
            artist_id = row[0]
            self.execute_sql((
                'DELETE FROM artist_2_artist WHERE artist1 = ? OR artist2 = '
                '?;', (artist_id, artist_id)), priority=10)
            self.execute_sql(
                ('DELETE FROM artists WHERE id = ?', (artist_id,)), priority=10)


class SimilarityService(dbus.service.Object):
    """Service that can be queried for similar songs."""

    def __init__(self, bus_name, object_path):
        import gst
        self.db = Db()
        self.lastfm = True
        self.cache_time = 90
        super(SimilarityService, self).__init__(
            bus_name=bus_name, object_path=object_path)
        self.mir = Mir()
        self.loop = gobject.MainLoop()

    def log(self, message):
        """Log message."""
        print message

    def get_similar_tracks_from_lastfm(self, artist_name, title, track_id):
        """Get similar tracks to the last one in the queue."""
        self.log("Getting similar tracks from last.fm for: %s - %s" % (
            artist_name, title))
        enc_artist_name = artist_name.encode("utf-8")
        enc_title = title.encode("utf-8")
        url = TRACK_URL % (
            urllib.quote_plus(enc_artist_name),
            urllib.quote_plus(enc_title))
        xmldoc = self.last_fm_request(url)
        if xmldoc is None:
            return []
        nodes = xmldoc.getElementsByTagName("track")
        results = []
        tracks_to_update = {}
        for node in nodes:
            similar_artist = similar_title = ''
            match = None
            for child in node.childNodes:
                if child.nodeName == 'artist':
                    similar_artist = child.getElementsByTagName(
                        "name")[0].firstChild.nodeValue.lower().decode('utf-8')
                elif child.nodeName == 'name':
                    similar_title = child.firstChild.nodeValue.lower().decode(
                        'utf-8')
                elif child.nodeName == 'match':
                    match = int(float(child.firstChild.nodeValue) * 100)
                if (similar_artist != '' and similar_title != ''
                    and match is not None):
                    break
            result = {
                'score': match,
                'artist': similar_artist,
                'title': similar_title}
            tracks_to_update.setdefault(track_id, []).append(result)
            results.append((match, similar_artist, similar_title))
        self.db.update_similar_tracks(tracks_to_update)
        return results

    def get_similar_artists_from_lastfm(self, artist_name, artist_id):
        """Get similar artists from lastfm."""
        self.log("Getting similar artists from last.fm for: %s " % artist_name)
        enc_artist_name = artist_name.encode("utf-8")
        url = ARTIST_URL % (
            urllib.quote_plus(enc_artist_name))
        xmldoc = self.last_fm_request(url)
        if xmldoc is None:
            return []
        nodes = xmldoc.getElementsByTagName("artist")
        results = []
        artists_to_update = {}
        for node in nodes:
            name = node.getElementsByTagName(
                "name")[0].firstChild.nodeValue.lower().decode('utf-8')
            match = 0
            matchnode = node.getElementsByTagName("match")
            if matchnode:
                match = int(float(matchnode[0].firstChild.nodeValue) * 100)
            result = {
                'score': match,
                'artist': name}
            artists_to_update.setdefault(artist_id, []).append(result)
            results.append((match, name))
        self.db.update_similar_artists(artists_to_update)
        return results

    @Throttle(WAIT_BETWEEN_REQUESTS)
    def last_fm_request(self, url):
        """Make an http request to last.fm."""
        if not self.lastfm:
            return None
        try:
            stream = urllib.urlopen(url)
        except Exception, e:            # pylint: disable=W0703
            self.log("Error: %s" % e)
            return None
        try:
            xmldoc = minidom.parse(stream).documentElement
            return xmldoc
        except Exception, e:            # pylint: disable=W0703
            self.log("Error: %s" % e)
            self.lastfm = False
            return None

    @method(dbus_interface=DBUS_IFACE, in_signature='s')
    def remove_track_by_filename(self, filename):
        """Remove tracks from database."""
        self.db.remove_track_by_filename(filename)

    @method(dbus_interface=DBUS_IFACE, in_signature='ss')
    def remove_track(self, artist, title):
        """Remove tracks from database."""
        self.db.remove_track(artist, title)

    @method(dbus_interface=DBUS_IFACE, in_signature='s')
    def remove_artist(self, artist):
        """Remove tracks from database."""
        self.db.remove_artist(artist)

    @method(dbus_interface=DBUS_IFACE, in_signature='sbasi')
    def analyze_track(self, filename, add_neighbours, exclude_filenames,
                      priority):
        """Perform mirage analysis of a track."""
        if not filename:
            return
        trackid_scms = self.db.get_track_from_filename(
            filename, priority=priority)
        if not trackid_scms:
            self.log("no mirage data found for %s, analyzing track" % filename)
            try:
                scms = self.mir.analyze(filename.encode('utf-8'))
            except (MatrixDimensionMismatchException, MfccFailedException,
                    IndexError), e:
                self.log(repr(e))
                return
            self.db.add_track(filename, scms, priority=priority)
            trackid = self.db.get_track_id(filename, priority=priority)
        else:
            trackid, scms = trackid_scms
        if not add_neighbours:
            return
        if self.db.has_scores(trackid, no=NEIGHBOURS, priority=priority):
            return
        self.db.add_neighbours(
            trackid, scms, exclude_filenames=exclude_filenames,
            neighbours=NEIGHBOURS, priority=priority)

    @method(dbus_interface=DBUS_IFACE, in_signature='s', out_signature='a(is)')
    def get_ordered_mirage_tracks(self, filename):
        """Get similar tracks by mirage acoustic analysis."""
        trackid = self.db.get_track_id(filename, priority=0)
        return self.db.get_neighbours(trackid)

    @method(dbus_interface=DBUS_IFACE, in_signature='ss',
            out_signature='a(iss)')
    def get_ordered_similar_tracks(self, artist_name, title):
        """Get similar tracks from last.fm/the database.

        Sorted by descending match score.

        """
        artist_name = artist_name
        title = title
        now = datetime.now()
        track = self.db.get_track_from_artist_and_title(artist_name, title)
        track_id, updated = track[0], track[3]
        if updated:
            updated = datetime(*strptime(updated, "%Y-%m-%d %H:%M:%S")[0:6])
            if updated + timedelta(self.cache_time) > now:
                self.log("Getting similar tracks from db for: %s - %s" % (
                    artist_name, title))
                return self.db.get_similar_tracks(track_id)
        return self.get_similar_tracks_from_lastfm(
            artist_name, title, track_id)

    @method(dbus_interface=DBUS_IFACE, in_signature='as',
            out_signature='a(is)')
    def get_ordered_similar_artists(self, artists):
        """Get similar artists from the database.

        Sorted by descending match score.

        """
        results = []
        now = datetime.now()
        for name in artists:
            artist_name = name
            result = None
            artist = self.db.get_artist(artist_name)
            artist_id, updated = artist[0], artist[2]
            if updated:
                updated = datetime(
                    *strptime(updated, "%Y-%m-%d %H:%M:%S")[0:6])
                if updated + timedelta(self.cache_time) > now:
                    self.log(
                        "Getting similar artists from db for: %s " %
                        artist_name)
                    result = self.db.get_similar_artists(artist_id)
            if not result:
                result = self.get_similar_artists_from_lastfm(
                    artist_name, artist_id)
            results.extend(result)
        results.sort(reverse=True)
        return results

    def run(self):
        self.loop.run()


def register_service(bus):
    """Try to register DBus service for making sure we run only one instance.

    Return True if succesfully registered, False if already running.
    """
    name = bus.request_name(DBUS_BUSNAME, dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
    return name != dbus.bus.REQUEST_NAME_REPLY_EXISTS


def publish_service(bus):
    """Publish the service on DBus."""
    print "publishing"
    bus_name = dbus.service.BusName(DBUS_BUSNAME, bus=bus)
    service = SimilarityService(bus_name=bus_name, object_path=DBUS_PATH)
    service.run()


def main():
    """Start the service if it is not already running."""
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    if register_service(bus):
        publish_service(bus)
