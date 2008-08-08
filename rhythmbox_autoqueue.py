# Copyright (C) 2007-2008 - Eric Casteleijn
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA.

import rb
import rhythmdb

from autoqueue import AutoQueueBase, SongBase

#XXX FILL IN FOR TESTING
GCONFPATH = ''

class Song(SongBase):
    """A wrapper object around rhythmbox song objects."""
    def __init__(self, song, db):
        self.song = song
        self.db = db
        
    def get_artist(self):
        """return lowercase UNICODE name of artist"""
        return self.db.entry_get(song, rhythmdb.PROP_ARTIST).lower()

    def get_title(self):
        """return lowercase UNICODE title of song"""
        return self.db.entry_get(song, rhythmdb.PROP_TITLE).lower()

    def get_tags(self):
        """return a list of tags for the songs"""
        return []
    

class AutoQueuePlugin(rb.Plugin, AutoQueueBase):
    def __init__(self):
        rb.Plugin.__init__(self)
        AutoQueueBase.__init__(self)
        self.cache = False
        
    def activate(self, shell):
        self.shell = shell
        self.db = shell.get_property('db')
        sp = shell.get_player ()
        self.pec_id = sp.connect(
            'playing-song-changed', self.playing_entry_changed)
        self.pc_id = sp.connect('playing-changed', self.playing_changed)
        
    def deactivate(self, shell):
        self.db = None
        self.shell = None
        sp = shell.get_player()
        sp.disconnect(self.pec_id)
        sp.disconnect(self.pc_id)

    def playing_changed(self, sp, playing):
        self.on_song_started(Song(sp.get_playing_entry(), self.db))

    def playing_entry_changed(self, sp, entry):
        self.on_song_started(Song(entry, self.db))
        
    def player_get_userdir(self):
        """get the application user directory to store files"""
        return GCONFPATH
    
    def player_construct_track_search(self, artist, title, restrictions):
        """construct a search that looks for songs with this artist
        and title"""
        return (rhythmdb.QUERY_PROP_EQUALS, rhythmdb.PROP_ARTIST, artist,
                rhythmdb.QUERY_PROP_EQUALS, rhythmdb.PROP_TITLE, title)
    
    def player_construct_tag_search(self, tags, exclude_artists, restrictions):
        """construct a search that looks for songs with these
        tags"""
        return None

    def player_construct_artist_search(self, artist, restrictions):
        """construct a search that looks for songs with this artist"""
        return (rhythmdb.QUERY_PROP_EQUALS, rhythmdb.PROP_ARTIST, artist)
        
    def player_construct_restrictions(
        self, track_block_time, relaxors, restrictors):
        """contstruct a search to further modify the searches"""
        return None

    def player_set_variables_from_config(self):
        """Initialize user settings from the configuration storage"""
        pass

    def player_get_queue_length(self):
        """Get the current length of the queue"""
        return 0

    def player_enqueue(self, song):
        """Put the song at the end of the queue"""
        self.shell.add_to_queue(
            self.db.entry_get(song.song, rhythmdb.PROP_LOCATION))

    def player_search(self, search):
        """perform a player search"""
        query = self.db.query_new()
        self.db.query_append(query, search)
        query_model = self.db.query_model_new_empty()
        self.db.do_full_query_parsed(query_model, query)
        result = []
        for row in query_model:
            result.append(Song(row[0], self.db))
        return result

    def player_get_songs_in_queue(self):
        """return (wrapped) song objects for the songs in the queue"""
        return []

