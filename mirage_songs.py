import os
import const
from datetime import datetime
from plugins.songsmenu import SongsMenuPlugin
from mirage import Mir, Db
from quodlibet.util import copool

def get_title(song):
    """return lowercase UNICODE title of song"""
    version = song.comma("version").lower()
    title = song.comma("title").lower()
    if version:
        return "%s (%s)" % (title, version)
    return title


class MirageSongsPlugin(SongsMenuPlugin):
    PLUGIN_ID = "Mirage Analysis"
    PLUGIN_NAME = _("Mirage Analysis")
    PLUGIN_DESC = _("Perform Mirage Analysis of the selected songs.")
    PLUGIN_ICON = "gtk-find-and-replace"
    PLUGIN_VERSION = "0.1"

    def __init__(self, *args):
        super(MirageSongsPlugin, self).__init__(*args)
        self.mir = Mir()
        self.dbpath = os.path.join(self.player_get_userdir(), "similarity.db")

    def player_get_userdir(self):
        """get the application user directory to store files"""
        try:
            return const.USERDIR
        except AttributeError:
            return const.DIR

    def do_stuff(self, songs):
        db = Db(self.dbpath)
        l = len(songs)
        for i, song in enumerate(songs):
            artist_name = song.comma("artist").lower()
            title = get_title(song)
            print "%03d/%03d %s - %s" % (i + 1, l, artist_name, title)
            filename = song("~filename")
            trackid_scms = db.get_track(filename)
            if not trackid_scms:
                try:
                    scms = self.mir.analyze(filename)
                except:
                    return
                db.add_track(filename, scms)
            yield
        print "done"

    def plugin_songs(self, songs):
        fid = "mirage_songs" + str(datetime.now())
        copool.add(self.do_stuff, songs, funcid=fid)
