"""Context awareness filters."""

import re
from datetime import datetime

EASTERS = {
    2014: datetime(2014, 4, 20),
    2015: datetime(2015, 4, 5),
    2016: datetime(2016, 3, 27),
    2017: datetime(2017, 4, 16),
    2018: datetime(2018, 4, 1),
    2019: datetime(2019, 4, 21),
    2020: datetime(2020, 4, 12),
    2021: datetime(2021, 4, 4),
    2022: datetime(2022, 4, 17)}

mar_21 = lambda year: datetime(year, 3, 21)
jun_21 = lambda year: datetime(year, 6, 21)
sep_21 = lambda year: datetime(year, 9, 21)
dec_21 = lambda year: datetime(year, 12, 21)


def escape(the_string):
    """Double escape quotes."""
    # TODO: move to utils
    return the_string.replace('"', '\\"').replace("'", "\\'")


class Context(object):

    """Object representing the current context."""

    def __init__(self, date, location, geohash, birthdays, last_song,
                 nearby_artists, southern_hemisphere, weather_tags,
                 extra_context):
        self.date = date
        self.location = location
        self.geohash = geohash
        self.birthdays = birthdays
        self.last_song = last_song
        self.nearby_artists = nearby_artists
        self.southern_hemisphere = southern_hemisphere
        self.weather_tags = weather_tags
        self.predicates = []
        self.extra_context = extra_context
        self.build_predicates()

    def adjust_score(self, result):
        """Adjust the score for the result if appropriate."""
        for predicate in self.predicates:
            if (predicate.applies_to_song(result['song'], exclusive=False)
                    and predicate.applies_in_context(self)):
                print repr(predicate), "adjusting positively", result['score']
                predicate.positive_score(result)
                print repr(predicate), "adjusted positively", result['score']
            elif (predicate.applies_to_song(result['song'], exclusive=True)
                    and not predicate.applies_in_context(self)):
                print repr(predicate), "adjusting negatively", result['score']
                predicate.negative_score(result)
                print repr(predicate), "adjusted negatively", result['score']

    def build_predicates(self):
        """Construct predicates to check against the context."""
        self.add_standard_predicates()
        self.add_december_predicate()
        self.add_location_predicates()
        self.add_weather_predicates()
        self.add_birthday_predicates()
        self.add_extra_predicates()
        self.add_last_song_predicates()
        self.add_nearby_artist_predicates()

    def add_nearby_artist_predicates(self):
        for artist in self.nearby_artists:
            self.predicates.append(ArtistPredicate(artist))

    def add_last_song_predicates(self):
        if self.last_song:
            self.predicates.append(
                TagsPredicate(self.last_song.get_non_geo_tags()))
            self.predicates.append(
                GeohashPredicate(self.last_song.get_geohashes()))

    def add_extra_predicates(self):
        if self.extra_context:
            for term in [l.strip().lower() for l in
                         self.extra_context.split(',')]:
                self.predicates.append(StringPredicate(term))

    def add_birthday_predicates(self):
        for name_date in self.birthdays.split(','):
            if not ':' in name_date:
                continue
            name, bdate = name_date.strip().split(':')
            bdate = bdate.strip()
            if '-' in bdate:
                bdate = [int(i) for i in bdate.split('-')]
            else:
                bdate = [int(i) for i in bdate.split('/')]
            if len(bdate) == 3:
                year, month, day = bdate
                age = self.date.year - year
                self.predicates.append(
                    BirthdayPredicate(
                        year=year, month=month, day=day, name=name, age=age))

    def add_weather_predicates(self):
        for weather_condition in self.weather_tags:
            self.predicates.append(
                StringPredicate(weather_condition))

    def add_location_predicates(self):
        if self.location:
            for location in self.location.split(','):
                self.predicates.append(
                    StringPredicate(location.strip().lower()))
        if self.geohash:
            self.predicates.append(GeohashPredicate([self.geohash]))

    def add_december_predicate(self):
        if self.date.month == 12:
            # December is for retrospection
            self.predicates.append(SongYearPredicate(self.date.year))

    def add_standard_predicates(self):
        self.predicates.extend(
            STATIC_PREDICATES + [
                YearPredicate(self.date.year),
                DatePredicate.from_date(self.date)])


class Predicate(object):

    terms = tuple()
    non_exclusive_terms = tuple()
    tag_only_terms = tuple()
    title_searches = None
    title_searches_non_exclusive = None
    tag_searches = None
    tag_searches_non_exclusive = None

    def __init__(self):
        self.build_searches()

    def build_searches(self):
        """Construct all the searches for this predicate."""
        self.title_searches = [
            self.build_title_search(term) for term in self.terms]
        self.title_searches_non_exclusive = [
            self.build_title_search(term) for term in self.non_exclusive_terms]
        self.tag_searches = [
            self.build_tag_search(term)
            for term in self.terms + self.tag_only_terms]
        self.tag_searches_non_exclusive = [
            self.build_tag_search(term) for term in self.non_exclusive_terms]

    def _build_search(self, term):
        return '%s(e?s)?' % (term,)

    def build_title_search(self, term):
        return re.compile(r'\b%s\b' % (self._build_search(term),))

    def build_tag_search(self, term):
        return re.compile('^%s$' % (self._build_search(term),))

    def get_title_searches(self, exclusive):
        """Get title searches for this predicate."""
        return self.title_searches if exclusive else (
            self.title_searches + self.title_searches_non_exclusive)

    def get_tag_searches(self, exclusive):
        """Get tag searches for this predicate."""
        return self.tag_searches if exclusive else (
            self.tag_searches + self.tag_searches_non_exclusive)

    def applies_to_song(self, song, exclusive):
        """Determine whether the predicate applies to the song."""
        title = song.get_title().lower()
        for search in self.get_title_searches(exclusive=exclusive):
            if search.match(title):
                return True
        for search in self.get_tag_searches(exclusive=exclusive):
            for tag in song.get_non_geo_tags():
                if search.match(tag):
                    return True
        return False

    def applies_in_context(self, context):
        return True

    def positive_score(self, result):
        result['score'] /= 2

    def negative_score(self, result):
        pass

    @classmethod
    def get_search_expressions(cls, modifier=''):
        searches = []
        multiples = '(e?s)?' if cls.multiples else ''
        for alternative in cls.get_search_terms():
            searches.extend([
                '%sgrouping=/^%s%s$/' % (modifier, alternative, multiples),
                '%stitle=/\\b%s%s\\b/' % (modifier, alternative, multiples)])
        for alternative in cls.tag_only_terms:
            searches.append(
                '%sgrouping=/^%s%s$/' % (modifier, alternative, multiples))
        return searches

    @classmethod
    def get_negative_search_expressions(cls):
        return cls.get_search_expressions(modifier='!')


class StringPredicate(Predicate):

    def __init__(self, term):
        self.terms = (term,)
        super(StringPredicate, self).__init__()


class ArtistPredicate(Predicate):

    def __init__(self, artist):
        self.artist = artist
        self.terms = (artist,)
        super(ArtistPredicate, self).__init__()

    def applies_to_song(self, song, exclusive):
        if self.artist.strip().lower() in [a.strip().lower()
                                           for a in song.get_artists()]:
            return True
        return super(ArtistPredicate, self).applies_to_song(song, exclusive)


class TagsPredicate(Predicate):

    def __init__(self, tags):
        self.tags = set(tags)
        super(TagsPredicate, self).__init__()

    def applies_to_song(self, song, exclusive):
        return set(song.get_non_geo_tags()) & self.tags

    def positive_score(self, result):
        song_tags = set(result['song'].get_non_geo_tags())
        score = (
            len(song_tags & self.tags) /
            float(len(song_tags | self.tags) + 1))
        result['score'] /= 1 + score


class GeohashPredicate(Predicate):

    def __init__(self, geohashes):
        self.geohashes = geohashes
        super(GeohashPredicate, self).__init__()

    def applies_to_song(self, song, exclusive):
        for self_hash in self.geohashes:
            for other_hash in song.get_geohashes():
                if other_hash.startswith(self_hash[:2]):
                    return True

        return False

    def positive_score(self, result):
        longest_common = 0
        for self_hash in self.geohashes:
            for other_hash in result['song'].get_geohashes():
                if self_hash[0] != other_hash[0]:
                    continue

                for i, character in enumerate(self_hash):
                    if i >= len(other_hash):
                        break

                    if character != other_hash[i]:
                        break

                    if i > longest_common:
                        longest_common = i
        result['score'] *= 1.0 / (2 ** longest_common)


class YearPredicate(Predicate):

    def __init__(self, year):
        self.tag_only_terms = (str(year),)
        super(YearPredicate, self).__init__()


class SongYearPredicate(YearPredicate):

    def applies_to_song(self, song, exclusive):
        return self.year == song.get_year()


class ExclusivePredicate(Predicate):

    def negative_score(self, result):
        result['score'] *= 2


class DatePredicate(ExclusivePredicate):

    day = None
    month = None

    @classmethod
    def from_date(cls, date):
        """Construct a DatePredicate from a datetime object."""
        new = cls()
        new.month = date.month
        new.day = date.day
        new.build_searches()
        return new

    def applies_in_context(self, context):
        date = context.date
        return date.day == self.day and date.month == self.month

    def build_searches(self):
        super(DatePredicate, self).build_searches()
        if self.month and self.day:
            self.tag_searches.append(
                self.build_tag_search("%02d-%02d" % (self.month, self.day)))


class SeasonPredicate(ExclusivePredicate):

    def negative_score(self, result):
        result['score'] *= (1 + 1 / 4.0)


class Winter(SeasonPredicate):

    terms = ('winter', 'wintertime')

    def applies_in_context(self, context):
        date = context.date
        southern_hemisphere = context.southern_hemisphere
        if (date >= dec_21(date.year) or date <= mar_21(date.year)
                and not southern_hemisphere):
            return True

        if (date >= jun_21(date.year) and date <= sep_21(date.year)
                and southern_hemisphere):
            return True

        return False


class Spring(SeasonPredicate):

    terms = ('spring', 'springtime')

    def applies_in_context(self, context):
        date = context.date
        southern_hemisphere = context.southern_hemisphere
        if (date >= mar_21(date.year) and date <= jun_21(date.year)
                and not southern_hemisphere):
            return True

        if (date >= sep_21(date.year) and date <= dec_21(date.year)
                and southern_hemisphere):
            return True

        return False


class Summer(SeasonPredicate):

    terms = ('summer', 'summertime')

    def applies_in_context(self, context):
        date = context.date
        southern_hemisphere = context.southern_hemisphere
        if (date >= jun_21(date.year) and date <= sep_21(date.year)
                and not southern_hemisphere):
            return True

        if (date >= dec_21(date.year) or date <= mar_21(date.year)
                and southern_hemisphere):
            return True

        return False


class Autumn(SeasonPredicate):

    terms = ('autumn',)
    tag_only_terms = ('fall',)

    def applies_in_context(self, context):
        date = context.date
        southern_hemisphere = context.southern_hemisphere
        if (date >= sep_21(date.year) and date <= dec_21(date.year)
                and not southern_hemisphere):
            return True

        if (date >= mar_21(date.year) and date <= jun_21(date.year)
                and southern_hemisphere):
            return True

        return False


class MonthPredicate(ExclusivePredicate):

    def applies_in_context(self, context):
        return context.date.month == self.month

    def negative_score(self, result):
        result['score'] *= (1 + 1 / 12.0)


class January(MonthPredicate):

    month = 1
    terms = ('january',)


class February(MonthPredicate):

    month = 2
    terms = ('february',)


class March(MonthPredicate):

    month = 3
    tag_only_terms = ('march',)


class April(MonthPredicate):

    month = 4
    terms = ('april',)


class May(MonthPredicate):

    month = 5
    tag_only_terms = ('may',)


class June(MonthPredicate):

    month = 6
    terms = ('june',)


class July(MonthPredicate):

    month = 7
    terms = ('july',)


class August(MonthPredicate):

    month = 8
    terms = ('august',)


class September(MonthPredicate):

    month = 9
    terms = ('september',)


class October(MonthPredicate):

    month = 10
    terms = ('october',)


class November(MonthPredicate):

    month = 11
    terms = ('november',)


class December(MonthPredicate):

    month = 12
    terms = ('december',)


class DayPredicate(ExclusivePredicate):

    def applies_in_context(self, context):
        return (
            context.date.isoweekday() == self.day_index and
            context.date.hour >= 4) or (
                context.date.isoweekday() == self.day_index + 1 and
                context.date.hour < 4)

    def negative_score(self, result):
        result['score'] *= (1 + 1 / 7.0)


class Monday(DayPredicate):

    day_index = 1
    terms = ('monday',)


class Tuesday(DayPredicate):

    day_index = 2
    terms = ('tuesday',)


class Wednesday(DayPredicate):

    day_index = 3
    terms = ('wednesday',)


class Thursday(DayPredicate):

    day_index = 4
    terms = ('thursday',)


class Friday(DayPredicate):

    day_index = 5
    terms = ('friday',)


class Saturday(DayPredicate):

    day_index = 6
    terms = ('saturday',)


class Sunday(DayPredicate):

    day_index = 7
    terms = ('sunday',)


class Night(ExclusivePredicate):

    terms = ('night',)

    def applies_in_context(self, context):
        date = context.date
        return date.hour >= 21 or date.hour < 4


class Evening(ExclusivePredicate):

    terms = ('evening',)

    def applies_in_context(self, context):
        date = context.date
        return date.hour >= 18 and date.hour < 21


class Morning(ExclusivePredicate):

    terms = ('morning',)

    def applies_in_context(self, context):
        date = context.date
        return date.hour >= 4 and date.hour < 12


class Afternoon(ExclusivePredicate):

    terms = ('afternoon',)

    def applies_in_context(self, context):
        date = context.date
        return date.hour >= 12 and date.hour < 18


class Weekend(ExclusivePredicate):

    terms = ('weekend',)

    def applies_in_context(self, context):
        date = context.date
        weekday = date.isoweekday()
        return weekday == 6 or weekday == 7 or (
            weekday == 5 and date.hour >= 17)


class Christmas(ExclusivePredicate):

    terms = ('christmas', 'santa claus', 'xmas')

    non_exclusive_terms = (
        'reindeer', 'sled', 'santa', 'snow', 'bell', 'jesus', 'eggnoc',
        'mistletoe', 'carol', 'nativity', 'mary', 'joseph', 'manger')

    def applies_in_context(self, context):
        date = context.date
        return date.month == 12 and date.day >= 20 and date.day <= 29


class Kwanzaa(ExclusivePredicate):

    terms = ('kwanzaa',)

    def applies_in_context(self, context):
        date = context.date
        return (date.month == 12 and date.day >= 26) or (
            date.month == 1 and date.day == 1)


class NewYear(ExclusivePredicate):

    terms = ('new year',)

    def applies_in_context(self, context):
        date = context.date
        return (date.month == 12 and date.day >= 27) or (
            date.month == 1 and date.day <= 7)


class Halloween(ExclusivePredicate):

    terms = ('halloween', 'hallowe\'en', 'all hallow\'s')
    non_exclusive_terms = (
        'haunt', 'haunting', 'haunted', 'ghost', 'monster', 'horror', 'devil',
        'witch', 'pumkin', 'bone', 'skeleton', 'ghosts', 'zombie', 'werewolf',
        'werewolves', 'vampire', 'evil', 'scare', 'scary', 'scaring', 'fear',
        'fright', 'blood', 'bat', 'dracula', 'spider', 'costume', 'satan',
        'hell', 'undead', 'dead', 'death', 'grave')

    def applies_in_context(self, context):
        date = context.date
        return (date.month == 10 and date.day >= 25) or (
            date.month == 11 and date.day < 2)


class EasterBased(ExclusivePredicate):

    def applies_in_context(self, context):
        date = context.date
        easter = EASTERS[date.year]
        if self.easter_offset(date, easter):
            return True

        return False

    def easter_offset(self, date, easter):
        return (date - easter).days == self.days_after_easter


class Easter(EasterBased):

    terms = ('easter',)
    non_exclusive_terms = ('egg', 'bunny', 'bunnies', 'rabbit')

    def easter_offset(self, date, easter):
        return abs(date - easter).days < 5


class MardiGras(EasterBased):

    terms = ('mardi gras', 'shrove tuesday', 'fat tuesday')
    days_after_easter = -47


class AshWednesday(EasterBased):

    terms = ('ash wednesday',)
    non_exclusive_terms = ('ash',)
    days_after_easter = -46


class PalmSunday(EasterBased):

    terms = ('palm sunday',)
    non_exclusive_terms = ('palms',)
    days_after_easter = -7


class MaundyThursday(EasterBased):

    terms = ('maundy thursday',)
    days_after_easter = -3


class GoodFriday(EasterBased):

    terms = ('good friday',)
    days_after_easter = -2


class Ascension(EasterBased):

    terms = ('ascension',)
    days_after_easter = 39


class Pentecost(EasterBased):

    terms = ('pentecost',)
    days_after_easter = 49


class WhitMonday(EasterBased):

    terms = ('whit monday',)
    days_after_easter = 50


class AllSaints(EasterBased):

    terms = ('all saints',)
    days_after_easter = 56


class VeteransDay(DatePredicate):

    terms = ('armistice day', 'veterans day')
    non_exclusive_terms = ('peace', 'armistice', 'veteran')
    month = 11
    day = 11


class Assumption(DatePredicate):

    terms = ('assumption',)
    month = 8
    day = 15


class IndependenceDay(DatePredicate):

    terms = ('independence day',)
    non_exclusive_terms = (
        'independence', 'united states', 'independant', 'usa', 'u.s.a.')
    month = 7
    day = 4


class GroundhogDay(DatePredicate):

    terms = ('groundhog day',)
    non_exclusive_terms = ('groundhog',)
    month = 2
    day = 2


class ValentinesDay(DatePredicate):

    terms = ('valentine',)
    non_exclusive_terms = ('heart', 'love')
    month = 2
    day = 14


class AprilFools(DatePredicate):

    terms = ('april fool',)
    non_exclusive_terms = ('prank', 'joke', 'fool', 'hoax')
    month = 4
    day = 1


class CincoDeMayo(DatePredicate):

    terms = ('cinco de mayo',)
    non_exclusive_terms = ('mexico',)
    month = 5
    day = 5


class Solstice(ExclusivePredicate):

    terms = ('solstice',)

    def applies_in_context(self, context):
        date = context.date
        return date.day == 21 and (date.month == 6 or date.month == 12)


class Friday13(ExclusivePredicate):

    terms = ('friday the 13th',)
    non_exclusive_terms = ('bad luck', 'superstition')

    def applies_in_context(self, context):
        date = context.date
        return date.day == 13 and date.isoweekday() == 5


class BirthdayPredicate(DatePredicate):

    def __init__(self, year, month, day, name, age):
        self.non_exclusive_terms = ('birthday', name, str(year), str(age))
        self.year = year
        self.month = month
        self.day = day
        self.non_exclusive_terms = (str(year),)
        super(BirthdayPredicate, self).__init__()

    def applies_to_song(self, song, exclusive):
        if not exclusive and self.year == song.get_year():
            return True
        return super(BirthdayPredicate, self).applies_to_song(song, exclusive)


STATIC_PREDICATES = [
    Christmas(), Kwanzaa(), NewYear(), Halloween(), Easter(),
    MardiGras(), AshWednesday(), PalmSunday(), MaundyThursday(),
    GoodFriday(), Ascension(), Pentecost(), WhitMonday(), AllSaints(),
    VeteransDay(), Assumption(), IndependenceDay(), GroundhogDay(),
    ValentinesDay(), AprilFools(), CincoDeMayo(), Solstice(),
    Friday13(), January(), February(), March(), April(), May(), June(),
    July(), August(), September(), October(), November(), December(),
    Monday(), Tuesday(), Wednesday(), Thursday(), Friday(), Saturday(),
    Sunday(), Weekend(), Spring(), Summer(), Autumn(), Winter(),
    Evening(), Morning(), Afternoon(), Night()]
