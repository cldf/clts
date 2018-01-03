# coding: utf-8
"""
Transcription System module for consistent IPA handling.
========================================

"""
from __future__ import unicode_literals
import re
import codecs

from csvw import TableGroup
import json
import attr
from six import string_types, text_type

from pyclts.util import pkg_path, nfd, norm, EMPTY
from pyclts.models import *


def itertable(table):
    "Auxiliary function for iterating over a data table."
    for item in table:
        res = {
            key.lower(): nfd(value) if isinstance(value, text_type) else value
            for key, value in item.items()}
        for extra in res.pop('extra', []):
            key, _, value = extra.strip().partition(':')
            res[key] = value
        yield res


def translate(string, source_system, target_system):
    return ' '.join(
        '{0}'.format(target_system.get(source_system[s], '?')) for s in string.split())


class TranscriptionSystem(object):
    """
    A transcription System."""
    def __init__(self, system='bipa'):
        """
        :param system: The name of a transcription system or a directory containing one.
        """
        if not system:
            raise ValueError("You cannot specify an empty transcription system")
        if isinstance(system, string_types):
            system = pkg_path('transcriptionsystems', system)
            if not (system.exists() and system.is_dir()):
                raise ValueError('unknown system: {0}'.format(system))

        if system.joinpath('metadata.json').exists():
            self.system = TableGroup.from_file(system.joinpath('metadata.json'))
        else:
            self.system = TableGroup.from_file(
                pkg_path('transcriptionsystems', 'transcription-system-metadata.json'))
            self.system._fname = system.joinpath('metadata.json')

        self._features = {'consonant': {}, 'vowel': {}, 'tone': {}}
        # dictionary for feature values, checks when writing elements from
        # write_order to make sure no output is doubled
        self._feature_values = {}

        # load the general features
        features = json.load(codecs.open(pkg_path(
            'transcriptionsystems', 'features.json').as_posix(), 'r', 'utf-8'))

        self.diacritics = dict(
            consonant={}, vowel={}, click={}, diphthong={}, tone={}, cluster={})
        for dia in itertable(self.system.tabledict['diacritics.tsv']):
            if not dia['alias'] and not dia['typography']:
                self._features[dia['type']][dia['value']] = dia['grapheme']
            # assign feature values to the dictionary
            self._feature_values[dia['value']] = dia['feature']
            #self.diacritics[dia['type']][dia['grapheme']] = {dia['feature']: dia['value']}
            self.diacritics[dia['type']][dia['grapheme']] = dia['value']

        self.sound_classes = {}
        self._columns = {}  # the basic column structure, to allow for rendering
        self._sounds = {}  # Sounds by grapheme
        self._covered = {}
        # check for unresolved aliased sounds
        aliases = []
        for cls in [Consonant, Vowel, Tone, Marker]:
            type_ = cls.__name__.lower()
            self.sound_classes[type_] = cls
            # store information on column structure to allow for rendering of a
            # sound in this form, which will make it easier to insert it when
            # finding generated sounds
            self._columns[type_] = [c['name'].lower() for c in \
                    self.system.tabledict['{0}s.tsv'.format(type_)].asdict(
                        )['tableSchema']['columns']]
            for l, item in enumerate(itertable(
                    self.system.tabledict['{0}s.tsv'.format(type_)])):
                if item['grapheme'] in self._sounds:
                    raise ValueError('duplicate grapheme in {0}:{1}: {2}'.format(
                        type_ + 's.tsv', l + 2, item['grapheme']))
                sound = cls(ts=self, **item)
                # make sure this does not take too long
                for key, value in item.items():
                    if key not in {'grapheme', 'note', 'alias'} and value and value not in self._feature_values:
                        self._feature_values[value] = key
                        if type_ != 'marker' and value not in features[type_][key]:
                            raise ValueError(
                                    "Your data contains unrecognized features ({0}: {1}, line {2}))".format(
                                        key, value, l+2))

                self._sounds[item['grapheme']] = sound
                if not sound.alias:
                    if sound.featureset in self._features:
                        raise ValueError('duplicate features in {0}:{1}: {2}'.format(
                            type_ + 's.tsv', l + 2, sound.name))
                    self._features[sound.featureset] = sound
                else:
                    aliases += [(l, sound.type, sound.featureset)]
        # check for consistency of aliases: if an alias has no counterpart, it
        # is orphaned and needs to be deleted or given an accepted non-aliased
        # sound
        if [x for x in aliases if x[2] not in self._features]:
            error = ', '.join([text_type(x[0]+2) +'/'+text_type(x[1]) for x in aliases if
                x[2] not in self._features])
            raise ValueError('Your dataset contains orphaned aliases in line(s) {0}'.format(error))

        # basic regular expression, used to match the basic sounds in the system.
        self._regex = None
        self._update_regex()

        # normalization data
        self._normalize = {
            norm(r['source']): norm(r['target'])
            for r in itertable(self.system.tabledict['normalize.tsv'])}

    def _update_regex(self):
        self._regex = re.compile('|'.join(
            map(re.escape, sorted(self._sounds, key=lambda x: len(x),
                reverse=True))))

    @property
    def id(self):
        return self.system._fname.parent.name

    def _norm(self, string):
        """Extended normalization: normalize by list of norm-characers, split
        by character "/"."""
        nstring = norm(string)
        if "/" in string:
            s, t = string.split('/')
            nstring = t
        return self.normalize(nstring)

    def normalize(self, string):
        """Normalize the string according to normalization list"""
        return ''.join([self._normalize.get(x, x) for x in nfd(string)])

    def _from_name(self, string):
        """Parse a sound from its name"""
        if frozenset(string.split(' ')) in self._features:
            return self._features[frozenset(string.split(' '))]
        components = string.split(' ')
        rest, sound_class = components[:-1], components[-1]
        if sound_class in ['diphthong', 'cluster']:
            if string.startswith('from ') and 'to ' in string:
                extension = {'diphthong': 'vowel', 'cluster':
                        'consonant'}[sound_class]
                string_ = ' '.join(string.split(' ')[1:-1])
                from_, to_ = string_.split(' to ')
                v1, v2 = frozenset(from_.split(' ')+[extension]), frozenset(
                        to_.split(' ')+[extension])
                if v1 in self._features and v2 in self._features:
                    s1, s2 = (self._features[v1], self._features[v2])
                    if sound_class == 'diphthong':
                        return Diphthong.from_sounds(s1+s2, s1, s2, self)
                    else:
                        return Cluster.from_sounds(s1+s2, s1, s2, self)
                else:
                    # try to generate the sounds if they are not there
                    s1, s2 = self._from_name(from_+' '+extension), self._from_name(
                            to_+' '+extension)
                    if not 'unknownsound' in (s1.type, s2.type):
                        if sound_class == 'diphthong':
                            return Diphthong.from_sounds(s1+s2, s1, s2, self)
                        else:
                            return Cluster.from_sounds(s1+s2, s1, s2, self)
                    raise ValueError('components could not be found in system')
            else:
                raise ValueError('name string is erroneously encoded')

        if sound_class not in self.sound_classes:
            raise ValueError('no sound class specified')

        args = {self._feature_values.get(comp, '?'): comp for comp in rest}
        if '?' in args:
            raise ValueError('string contains unknown features')
        args['grapheme'] = ''
        args['ts'] = self
        sound = self.sound_classes[sound_class](**args)
        if sound.featureset not in self._features:
            sound.generated = True
            return sound
        return self._features[sound.featureset]

    def _parse(self, string):
        """Parse a string and return its features.

        :param string: A one-symbol string in NFD

        Notes
        -----
        Strategy is rather simple: we determine the base part of a string and
        then search left and right of this part for the additional features as
        expressed by the diacritics. Fails if a segment has more than one basic
        part.
        """
        nstring = self._norm(string)

        # check whether sound is in self.sounds
        if nstring in self._sounds:
            sound = self._sounds[nstring]
            sound.normalized = nstring != string
            sound.source = string
            return sound

        match = list(self._regex.finditer(nstring))
        # if the match has length 2, we assume that we have two sounds, so we split
        # the sound and pass it on for separate evaluation (recursive function)
        if len(match) == 2:
            sound1 = self._parse(nstring[:match[1].start()])
            sound2 = self._parse(nstring[match[1].start():])
            # if we have ANY unknown sound, we mark the whole sound as unknown, if
            # we have two known sounds of the same type (vowel or consonant), we
            # either construct a diphthong or a cluster
            if 'unknownsound' not in (sound1.type, sound2.type) and \
                    sound1.type == sound2.type:
                # diphthong creation
                if sound1.type == 'vowel':
                    return Diphthong.from_sounds(string, sound1, sound2, self)
                elif sound1.type == 'consonant' and \
                        sound1.manner in ('stop', 'implosive', 'click', 'nasal') and \
                        sound2.manner in ('stop', 'implosive', 'affricate', 'fricative'):
                    return Cluster.from_sounds(string, sound1, sound2, self)
            return UnknownSound(grapheme=nstring, source=string, ts=self)

        if len(match) != 1:
            # Either no match or more than one; both is considered an error.
            return UnknownSound(grapheme=nstring, source=string, ts=self)

        pre, mid, post = nstring.partition(nstring[match[0].start():match[0].end()])
        base_sound = self._sounds[mid]

        # A base sound with diacritics or a custom symbol.
        features = attr.asdict(base_sound)
        features.update(
            source=string,
            generated=True,
            normalized=nstring != string,
            base=base_sound.grapheme)
        
        # we construct two versions: the "normal" version and the version where
        # we search for aliases and normalize them (as our features system for
        # diacritics may well define aliases
        grapheme, sound = '', ''
        for dia in [p + EMPTY for p in pre]:
            feature = self.diacritics[base_sound.type].get(dia, {})
            if not feature:
                return UnknownSound(grapheme=nstring, source=string, ts=self)
            features[self._feature_values[feature]] = feature
            # we add the unaliased version to the grapheme
            grapheme += dia[0]
            # we add the corrected version (if this is needed) to the sound
            sound += self._features[base_sound.type][feature][0]
        # add the base sound
        grapheme += base_sound.grapheme
        sound += base_sound.s
        for dia in [EMPTY + p for p in post]:
            feature = self.diacritics[base_sound.type].get(dia, {})
            # we are strict: if we don't know the feature, it's an unknown
            # sound
            if not feature:
                return UnknownSound(grapheme=nstring, source=string, ts=self)
            features[self._feature_values[feature]] = feature
            grapheme += dia[1]
            sound += self._features[base_sound.type][feature][1]

        features['grapheme'] = sound
        new_sound = self.sound_classes[base_sound.type](**features)
        # check whether grapheme differs from re-generated sound
        if text_type(new_sound) != sound:
            new_sound.alias = True
        if grapheme != sound:
            new_sound.alias = True
            new_sound.grapheme = grapheme
        return new_sound

    def __getitem__(self, string):
        if isinstance(string, Sound):
            return self._features[string.featureset]
        if set(string.split(' ')).intersection(set(list(self.sound_classes)+['diphthong',
                'cluster'])) :
            return self._from_name(string)
        string = nfd(string)
        return self._parse(string)

    def __contains__(self, item):
        if isinstance(item, Sound):
            return item.featureset in self._features
        return item in self._sounds

    def __iter__(self):
        return iter(self._sounds)

    def items(self):
        return iter(self._sounds.items())

    def get(self, string, default=None):
        """Similar to the get method for dictionaries."""
        out = self[string]
        if out.type == 'unknownsound':
            return default
        return out

    def __call__(self, string, text=False):
        res = [
            self.get(s)
            for s in (string.split(' ') if isinstance(string, text_type) else string)]
        return ' '.join('{0}'.format(s) for s in res) if text else res
