"""Audit logic for normalized media items.

This module evaluates :class:`models.MediaItem` objects and returns structured
findings. It operates only on application models and helper functions from
``media.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
import itertools
import logging
import re
import unicodedata

from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from media import expected_episode_numbers_from_filename
from media import expected_episode_title_from_filename
from media import expected_movie_title_from_filename
from media import get_primary_audio_codec
from media import get_video_codec
from media import has_english_subtitles
from media import has_jellyfin_primary_image
from media import local_backdrop_exists
from models import MediaItem
from tvdb import TvdbEpisode


_APOSTROPHE_PATTERN = re.compile(r"['‘’]")
_GENERIC_PUNCTUATION_PATTERN = re.compile(r"[^\w\s/]")
# Periods and dashes are the punctuation marks that commonly appear directly
# between two letters with no surrounding space, rather than between two
# separate words - an abbreviation ("A.M.") or a hyphenated compound
# ("Break-Ups"). Since normalized_title() turns each into a space (needed
# so e.g. "S.W.A.T." lines up with a filename's own dot-delimited "S W A
# T"), a title on the other side that instead runs the same word together
# with no separator at all ("AM", "Breakups") won't match on that reading -
# so titles_match() also tries deleting these outright, closing the letters
# back up.
_COMPOUND_PUNCTUATION_PATTERN = re.compile(r"[.\-‐‑‒–—]")
_ROMAN_NUMERAL_PAREN_PATTERN = re.compile(r"\(([IVXLCDMivxlcdm]+)\)")
_ROMAN_NUMERAL_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_NUMERIC_PAREN_PATTERN = re.compile(r"\(\s*\d+\s*\)")
_PART_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|"
    "eleven|twelve|thirteen|fourteen|fifteen"
)
# A bare run of only roman-numeral letters (I, V, X, L, C, D, M), bounded by
# \b so it can only match a whole word, never a fragment of one (e.g. the
# "ic" inside "Ecstatic" is one, but \b won't allow the match to start
# there, since the preceding "t" is also a word character). Deliberately
# generic - it accepts any value, not just a small enumerated range - since
# TheTVDB and Jellyfin between them use roman numerals well past what any
# fixed list would practically cover (e.g. Star Wars: Clone Wars (2003)
# numbers episodes "Chapter I" through "Chapter XXV"). The unavoidable
# tradeoff of a generic match like this is an ordinary English word that
# happens to be spelled entirely with these seven letters (e.g. "Mix",
# "Civil", "Did", or the pronoun "I" itself) reading as a numeral too - see
# titles_match() for why every use of this pattern accepts that tradeoff.
_ROMAN_NUMERAL_SHAPE = "[IVXLCDM]+"
_PART_NUMBER_PATTERN = re.compile(
    rf"\b(?:part|pt)\.?\s+(?:\d+|{_PART_NUMBER_WORDS}|{_ROMAN_NUMERAL_SHAPE})\b",
    re.IGNORECASE,
)
# A trailing roman numeral with nothing else after it (e.g. "Chapter I") is
# the same disambiguator TheTVDB and Jellyfin metadata sometimes spell
# differently - one side numbering an episode titled just "Chapter" with an
# arabic digit ("Chapter 1"), the other with a roman numeral. Applied to
# normalized_title()'s already-casefolded output, so a lowercase-only class
# is enough (unlike _PART_NUMBER_PATTERN above, matched before casefolding).
_TRAILING_ROMAN_NUMERAL_PATTERN = re.compile(r"\b[ivxlcdm]+$")
_ARTICLE_STOPWORD_PATTERN = re.compile(r"\b(?:a|an|the)\b", re.IGNORECASE)

# Matches a character from a script no genuine English title would contain.
# TheTVDB's English translation (see tvdb.EPISODE_LANGUAGE) silently falls
# back to a series' original-language name for any episode without one on
# file - there's no separate flag in the API response saying that happened,
# so a name in a script like this is the practical signal that it did. Not
# exhaustive of every non-English script (accented Latin text, e.g. French
# or German, isn't covered - that's still closely comparable to English and
# not the source of the false-positive mismatches this guards against), just
# the common ones for named entities on TheTVDB.
_NON_ENGLISH_SCRIPT_PATTERN = re.compile(
    "["
    "Ͱ-Ͽ"  # Greek
    "Ѐ-ӿ"  # Cyrillic
    "֐-׿"  # Hebrew
    "؀-ۿ"  # Arabic
    "ऀ-ॿ"  # Devanagari
    "฀-๿"  # Thai
    "぀-ヿ"  # Hiragana, Katakana
    "㐀-䶿"  # CJK Unified Ideographs Extension A
    "一-鿿"  # CJK Unified Ideographs
    "가-힣"  # Hangul Syllables
    "]"
)

# Maps a British spelling to its American equivalent, for titles_match()'s
# spelling-insensitive comparison. Deliberately a curated word list rather
# than a general suffix rule (e.g. blindly rewriting a trailing "-our" to
# "-or", or "-oe-"/"-ae-" to "-e-") - those rules are attractive for their
# coverage, but collide with ordinary English words that only coincidentally
# share the ending ("hour", "tour", "four", "your" all end in "-our" without
# being spelling variants of anything; "shoe" would become the unrelated word
# "she" under a blind "-oe-" -> "-e-" rule). A word list only ever touches
# the specific words it lists, so it can't manufacture a false match between
# two otherwise-unrelated titles. Keys and lookups are lowercase, matching
# normalized_title()'s casefolded output.
_BRITISH_TO_AMERICAN_SPELLINGS: dict[str, str] = {
    # -our / -or
    "colour": "color", "colours": "colors", "coloured": "colored", "colouring": "coloring",
    "favour": "favor", "favours": "favors", "favoured": "favored", "favouring": "favoring",
    "favourite": "favorite", "favourites": "favorites",
    "honour": "honor", "honours": "honors", "honoured": "honored", "honouring": "honoring",
    "honourable": "honorable",
    "neighbour": "neighbor", "neighbours": "neighbors", "neighbourhood": "neighborhood",
    "neighbourly": "neighborly",
    "humour": "humor", "humours": "humors", "humoured": "humored", "humouring": "humoring",
    "rumour": "rumor", "rumours": "rumors", "rumoured": "rumored", "rumouring": "rumoring",
    "labour": "labor", "labours": "labors", "laboured": "labored", "labouring": "laboring",
    "behaviour": "behavior", "behaviours": "behaviors",
    "endeavour": "endeavor", "endeavours": "endeavors", "endeavoured": "endeavored",
    "harbour": "harbor", "harbours": "harbors", "harboured": "harbored",
    "armour": "armor", "armoured": "armored", "armoury": "armory",
    "valour": "valor", "vapour": "vapor", "vapours": "vapors",
    "saviour": "savior", "saviours": "saviors",
    "flavour": "flavor", "flavours": "flavors", "flavoured": "flavored", "flavouring": "flavoring",
    "glamour": "glamor", "parlour": "parlor", "parlours": "parlors",
    "rigour": "rigor", "vigour": "vigor", "ardour": "ardor", "candour": "candor",
    "clamour": "clamor", "splendour": "splendor", "tumour": "tumor", "tumours": "tumors",
    "odour": "odor", "odours": "odors", "demeanour": "demeanor",
    "misdemeanour": "misdemeanor", "misdemeanours": "misdemeanors",
    # -re / -er
    "theatre": "theater", "theatres": "theaters",
    "centre": "center", "centres": "centers", "centred": "centered", "centring": "centering",
    "metre": "meter", "metres": "meters", "litre": "liter", "litres": "liters",
    "fibre": "fiber", "fibres": "fibers", "calibre": "caliber",
    "sombre": "somber", "spectre": "specter", "spectres": "specters",
    "lustre": "luster", "sabre": "saber", "mitre": "miter",
    "sceptre": "scepter", "manoeuvre": "maneuver", "manoeuvres": "maneuvers",
    "manoeuvring": "maneuvering", "manoeuvred": "maneuvered",
    # -ise / -ize (and -isation / -ization)
    "organise": "organize", "organised": "organized", "organising": "organizing",
    "organisation": "organization", "organisations": "organizations",
    "realise": "realize", "realised": "realized", "realising": "realizing",
    "realisation": "realization",
    "recognise": "recognize", "recognised": "recognized", "recognising": "recognizing",
    "apologise": "apologize", "apologised": "apologized", "apologising": "apologizing",
    "analyse": "analyze", "analysed": "analyzed", "analysing": "analyzing",
    "criticise": "criticize", "criticised": "criticized", "criticising": "criticizing",
    "capitalise": "capitalize", "capitalised": "capitalized",
    "characterise": "characterize", "characterised": "characterized",
    "memorise": "memorize", "memorised": "memorized",
    "categorise": "categorize", "categorised": "categorized",
    "emphasise": "emphasize", "emphasised": "emphasized",
    "familiarise": "familiarize", "familiarised": "familiarized",
    "finalise": "finalize", "finalised": "finalized",
    "generalise": "generalize", "generalised": "generalized",
    "idolise": "idolize", "idolised": "idolized",
    "legalise": "legalize", "legalised": "legalized",
    "localise": "localize", "localised": "localized",
    "mobilise": "mobilize", "mobilised": "mobilized",
    "modernise": "modernize", "modernised": "modernized",
    "moralise": "moralize", "moralised": "moralized",
    "neutralise": "neutralize", "neutralised": "neutralized",
    "normalise": "normalize", "normalised": "normalized",
    "patronise": "patronize", "patronised": "patronized",
    "penalise": "penalize", "penalised": "penalized",
    "personalise": "personalize", "personalised": "personalized",
    "prioritise": "prioritize", "prioritised": "prioritized",
    "privatise": "privatize", "privatised": "privatized",
    "publicise": "publicize", "publicised": "publicized",
    "randomise": "randomize", "randomised": "randomized",
    "socialise": "socialize", "socialised": "socialized",
    "sterilise": "sterilize", "sterilised": "sterilized",
    "stigmatise": "stigmatize", "stigmatised": "stigmatized",
    "summarise": "summarize", "summarised": "summarized",
    "symbolise": "symbolize", "symbolised": "symbolized",
    "sympathise": "sympathize", "sympathised": "sympathized",
    "terrorise": "terrorize", "terrorised": "terrorized",
    "utilise": "utilize", "utilised": "utilized",
    "victimise": "victimize", "victimised": "victimized",
    "visualise": "visualize", "visualised": "visualized",
    # -ce / -se
    "defence": "defense", "defences": "defenses",
    "offence": "offense", "offences": "offenses",
    "licence": "license", "licences": "licenses",
    "pretence": "pretense", "pretences": "pretenses",
    "practise": "practice", "practised": "practiced", "practising": "practicing",
    # ae/oe -> e (academic and medical terms)
    "encyclopaedia": "encyclopedia", "encyclopaedias": "encyclopedias",
    "paediatric": "pediatric", "paediatrics": "pediatrics", "paediatrician": "pediatrician",
    "aesthetic": "esthetic", "aesthetics": "esthetics",
    "anaesthesia": "anesthesia", "anaesthetic": "anesthetic",
    "archaeology": "archeology", "archaeologist": "archeologist",
    "diarrhoea": "diarrhea", "foetus": "fetus", "foetal": "fetal",
    "gynaecology": "gynecology", "gynaecologist": "gynecologist",
    "haemoglobin": "hemoglobin", "haemorrhage": "hemorrhage", "haemophilia": "hemophilia",
    "leukaemia": "leukemia", "oesophagus": "esophagus",
    "orthopaedic": "orthopedic", "orthopaedics": "orthopedics",
    "amoeba": "ameba", "amoebas": "amebas",
    # Standalone spelling differences
    "grey": "gray", "greys": "grays", "greying": "graying",
    "mould": "mold", "moulds": "molds", "moulded": "molded", "moulding": "molding",
    "moult": "molt", "moulting": "molting",
    "smoulder": "smolder", "smouldering": "smoldering",
    "sulphur": "sulfur", "sulphate": "sulfate", "tyre": "tire", "tyres": "tires",
    "kerb": "curb", "kerbs": "curbs",
    "jewellery": "jewelry", "jeweller": "jeweler", "jewellers": "jewelers",
    "jewelled": "jeweled",
    "plough": "plow", "ploughs": "plows", "ploughed": "plowed", "ploughing": "plowing",
    "draught": "draft", "draughts": "drafts",
    "storey": "story", "storeys": "stories",
    "programme": "program", "programmes": "programs",
    "cheque": "check", "cheques": "checks",
    "cosy": "cozy", "cosier": "cozier", "cosiest": "coziest",
    "artefact": "artifact", "artefacts": "artifacts",
    "doughnut": "donut", "doughnuts": "donuts",
    "aeroplane": "airplane", "aeroplanes": "airplanes",
    "aluminium": "aluminum", "yoghurt": "yogurt",
    "moustache": "mustache", "moustaches": "mustaches",
    "pyjamas": "pajamas", "omelette": "omelet", "omelettes": "omelets",
    "catalogue": "catalog", "catalogues": "catalogs",
    "dialogue": "dialog", "dialogues": "dialogs",
    "analogue": "analog", "analogues": "analogs",
    "speciality": "specialty", "specialities": "specialties",
    "travelling": "traveling", "travelled": "traveled", "traveller": "traveler",
    "travellers": "travelers",
    "cancelled": "canceled", "cancelling": "canceling",
    "labelled": "labeled", "labelling": "labeling", "labeller": "labeler",
    "modelling": "modeling", "modelled": "modeled",
    "signalling": "signaling", "signalled": "signaled",
    "fuelled": "fueled", "fuelling": "fueling",
    "counsellor": "counselor", "counsellors": "counselors",
    "marvellous": "marvelous",
    "woollen": "woolen",
}

_MISMATCHED_TVDB_SERIES_MIN_EPISODES = 5
_MISMATCHED_TVDB_SERIES_MIN_UNMATCHED_RATIO = 0.5
_GOOD_TVDB_MATCH_MAX_UNMATCHED_RATIO = 0.1

# Dedicated logger for mismatched_tvdb_series()'s per-series matching data, so
# a user auditing a false positive/negative can see exactly which local
# episodes did and didn't line up with TheTVDB. Kept off the root logger's
# console handler (propagate=False) since this is per-episode-verbose - it
# only produces output once auditor.py attaches a file handler for it, kept
# in mismatched_tvdb_series.log.
LOGGER = logging.getLogger("mismatched_tvdb_series")
LOGGER.propagate = False


def audit_media_item(item: MediaItem) -> tuple[AuditFinding, ...]:
    """Run all media item audits and collect findings.

    Args:
        item: Media item to evaluate.

    Returns:
        A tuple containing every finding produced for the media item.
    """
    audits = (
        missing_english_subtitles,
        missing_backdrop,
        missing_primary_image,
        missing_episode_number,
        unknown_video_codec,
        unknown_audio_codec,
        mismatched_episode_filename_title,
        mismatched_movie_filename_title,
    )
    findings: list[AuditFinding] = []

    for audit in audits:
        finding = audit(item)
        if finding is not None:
            findings.append(finding)

    return tuple(findings)


def audit_library_items(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
    dvd_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Run library-level audits that require multiple media items.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number), as fetched for
            :func:`audit_episode_ordering`. When given, missing-season and
            missing-episode detection are checked against each series' full
            TheTVDB season/episode list instead of only gaps between
            locally-present numbers - except for a series flagged by
            :func:`mismatched_tvdb_series`, where that TheTVDB data is
            itself unreliable, so those two checks fall back to local-gap
            detection for it instead of reporting a wall of nonsense
            missing seasons/episodes on top of the mismatch finding.
        dvd_positions: TheTVDB DVD-order episodes for each series name, in
            the same shape as ``aired_positions``. Passed through to
            :func:`mismatched_tvdb_series` so a series numbered in DVD order
            on disk isn't flagged as a wrong TheTVDB match.

    Returns:
        A tuple containing findings derived from gaps across TV episodes.
    """
    items_tuple = tuple(items)
    findings: list[AuditFinding] = []
    mismatched_series_findings = mismatched_tvdb_series(items_tuple, aired_positions, dvd_positions)
    mismatched_series_names = frozenset(
        finding.media_item.series_name
        for finding in mismatched_series_findings
        if finding.media_item.series_name
    )
    trustworthy_aired_positions = (
        {
            series_name: positions
            for series_name, positions in aired_positions.items()
            if series_name not in mismatched_series_names
        }
        if aired_positions
        else aired_positions
    )
    trustworthy_dvd_positions = (
        {
            series_name: positions
            for series_name, positions in dvd_positions.items()
            if series_name not in mismatched_series_names
        }
        if dvd_positions
        else dvd_positions
    )
    findings.extend(missing_tv_series_seasons(items_tuple, trustworthy_aired_positions))
    findings.extend(missing_tv_season_episodes(items_tuple, trustworthy_aired_positions))
    findings.extend(
        mismatched_tvdb_title(items_tuple, trustworthy_aired_positions, trustworthy_dvd_positions)
    )
    findings.extend(mismatched_series_findings)
    return tuple(findings)


def missing_english_subtitles(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no configured English subtitles exist.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when English subtitles are present.
    """
    if has_english_subtitles(item):
        return None

    return _finding(
        item,
        category=AuditCategory.SUBTITLES,
        severity=AuditSeverity.WARNING,
        check_name="missing_english_subtitles",
        message="No configured English subtitles were found.",
    )


def missing_backdrop(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no local backdrop exists.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when a local backdrop exists.
    """
    if local_backdrop_exists(item):
        return None

    return _finding(
        item,
        category=AuditCategory.ARTWORK,
        severity=AuditSeverity.INFO,
        check_name="missing_backdrop",
        message="No local backdrop file was found.",
    )


def missing_primary_image(item: MediaItem) -> AuditFinding | None:
    """Return a finding when Jellyfin has no primary image for the item.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when Jellyfin reports a primary
        image.
    """
    if has_jellyfin_primary_image(item):
        return None

    return _finding(
        item,
        category=AuditCategory.ARTWORK,
        severity=AuditSeverity.INFO,
        check_name="missing_primary_image",
        message="No Jellyfin primary image was found.",
    )


def missing_episode_number(item: MediaItem) -> AuditFinding | None:
    """Return a finding when an episode has no episode number set.

    Unlike missing_tv_season_episodes, which flags numeric gaps between
    episodes that already have numbers, this catches an episode file Jellyfin
    could not assign a number to at all (episode_number is None) - the kind
    of gap apply_episode_numbers.py can fill in from TheTVDB's aired order.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the item is not an episode or
        already has an episode number.
    """
    if not item.is_episode or item.episode_number is not None:
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.WARNING,
        check_name="missing_episode_number",
        message="No episode number is set.",
    )


def unknown_video_codec(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the primary video codec is missing or unknown.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the video codec is known.
    """
    codec = get_video_codec(item)
    if codec not in {None, "unknown"}:
        return None

    return _finding(
        item,
        category=AuditCategory.VIDEO,
        severity=AuditSeverity.WARNING,
        check_name="unknown_video_codec",
        message="The primary video codec is missing or unknown.",
    )


def unknown_audio_codec(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the primary audio codec is missing.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when an audio codec exists.
    """
    if get_primary_audio_codec(item) is not None:
        return None

    return _finding(
        item,
        category=AuditCategory.AUDIO,
        severity=AuditSeverity.WARNING,
        check_name="unknown_audio_codec",
        message="No primary audio codec was found.",
    )


def mismatched_episode_filename_title(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the filename implies a different episode title.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the filename has no discernible
        episode title, or its implied title matches the metadata title.
    """
    expected_title = expected_episode_title_from_filename(item)
    if expected_title is None:
        return None

    if titles_match(expected_title, item.title):
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.WARNING,
        check_name="mismatched_episode_filename_title",
        message=(
            f'Filename suggests episode title "{expected_title}" but metadata '
            f'title is "{item.title}".'
        ),
    )


def mismatched_movie_filename_title(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the filename implies a different movie title.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the filename has no discernible
        movie title, or its implied title matches the metadata title.
    """
    expected_title = expected_movie_title_from_filename(item)
    if expected_title is None:
        return None

    if titles_match(expected_title, item.title):
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.WARNING,
        check_name="mismatched_movie_filename_title",
        message=(
            f'Filename suggests movie title "{expected_title}" but metadata '
            f'title is "{item.title}".'
        ),
    )


def normalized_title(value: str) -> str:
    """Return a normalized title for filename/metadata comparison.

    An accented letter (e.g. "é", "ü", "ñ") is folded to its plain-letter
    equivalent ("e", "u", "n"), since Jellyfin metadata and filenames don't
    always agree on whether a title is spelled with its original diacritics
    or a plain-ASCII approximation (e.g. "Café" versus "Cafe", "Degüello"
    versus "Deguello"). Unicode decomposes an accented letter into the plain
    letter plus a separate combining-mark character for exactly this reason;
    dropping that mark is enough, no per-character accent table needed.

    Any punctuation (commas, periods, dashes, exclamation points, double
    quotes, parentheses, and so on) is treated as word-separating whitespace
    rather than deleted outright, so e.g. an abbreviated title like
    "S.W.A.T." compares equal to its filename counterpart instead of
    collapsing into a run-together "swat", and a hyphenated title like
    "Spider-Man" compares equal to a space-separated "Spider Man". Two
    exceptions: "/" is exempted, since it's still meaningful as a
    multi-episode title separator (see below); and an apostrophe is deleted
    outright rather than spaced, since it's normally part of a contraction or
    possessive (e.g. "Lover's Walk") where a title on the other side just as
    often drops it rather than spelling it with a space (e.g. "Lovers Walk"
    - not "Lover s Walk").
    Parenthesized roman numerals (e.g. "(I)") are converted to their
    arabic-numeral equivalent (e.g. "(1)") before being dropped, same as any
    other purely numeric parenthetical (e.g. "(1)", "(2016)") - these are
    disambiguators (a multi-part episode's part number, or a movie's release
    year) rather than title text, and Jellyfin metadata and filenames don't
    agree on whether or how to include them. A "Part 1" / "Pt 1" / "Part One"
    / "Part I" style suffix (arabic digit, spelled-out word, or roman
    numeral) is dropped for the same reason, so it lines up with a
    same-numbered parenthetical disambiguator instead of being flagged as a
    mismatch. "&" is treated the same as "and", and "+" the same as "/",
    since Jellyfin sometimes converts between these when deriving filenames
    from metadata. Once every "/"-separated segment is normalized, an entry
    that's an exact repeat of the one right before it is dropped - a
    multi-part episode whose parts share one underlying title (distinguished
    only by the part-number disambiguator already stripped above) otherwise
    stays duplicated on the metadata side, e.g. "Title (1) / Title (2)",
    while the filename side names it just once.

    A leading article isn't dropped here, unlike :func:`titles_match` -
    callers needing an exact match to still win over a same-titled-except-
    for-an-article coincidence (e.g. apply_episode_numbers.py's strict/loose
    fallback) rely on this function preserving articles as-is.

    See :func:`titles_match` for comparing two titles that may use "," and
    "/" as different, but equivalent, ways to join a multi-episode file's
    individual episode titles, and that also ignores "a"/"an"/"the".
    """
    decomposed_value = unicodedata.normalize("NFKD", value)
    normalized_value = "".join(
        character for character in decomposed_value if not unicodedata.combining(character)
    )
    normalized_value = _ROMAN_NUMERAL_PAREN_PATTERN.sub(_roman_numeral_paren_to_arabic, normalized_value)
    normalized_value = _PART_NUMBER_PATTERN.sub("", normalized_value)
    normalized_value = _NUMERIC_PAREN_PATTERN.sub("", normalized_value)
    normalized_value = normalized_value.replace("&", " and ")
    normalized_value = normalized_value.replace("+", "/")
    normalized_value = _APOSTROPHE_PATTERN.sub("", normalized_value)
    normalized_value = _GENERIC_PUNCTUATION_PATTERN.sub(" ", normalized_value)
    normalized_value = re.sub(r"\s+", " ", normalized_value)
    normalized_value = normalized_value.strip().casefold()
    return _collapse_duplicate_segments(normalized_value)


def _collapse_duplicate_segments(value: str, *, separator: str = "/") -> str:
    """Return ``value`` with immediately-repeated ``separator``-joined segments merged."""
    segments = [segment.strip() for segment in value.split(separator)]
    segments = [segment for segment in segments if segment]

    deduped: list[str] = []
    for segment in segments:
        if not deduped or segment != deduped[-1]:
            deduped.append(segment)

    return f" {separator} ".join(deduped)


def titles_match(first: str, second: str) -> bool:
    """Return whether two titles are equal for filename/metadata comparison.

    A multi-episode file's combined title joins its individual episodes'
    titles together, but Jellyfin metadata and filenames don't agree on the
    separator - metadata typically uses "/" (e.g. "Title A / Title B") while
    a filename someone hand-named often uses "," instead (e.g.
    "Title A, Title B"). A comma can't simply be treated as always meaning
    "/", though, since it's also perfectly ordinary punctuation within a
    single, un-joined title (e.g. "Poltergeist, Part One") - so each title is
    compared both as literally normalized and with its commas swapped for
    slashes first.

    Each of those readings is also compared both as-is and with every period
    or dash deleted outright rather than turned into a space by
    :func:`normalized_title` - an abbreviation like "A.M." needs to collapse
    into "am" to match an unpunctuated "AM" on the other side, and a
    hyphenated compound like "Break-Ups" needs to collapse into "breakups"
    to match "Breakups", the same way :func:`normalized_title` already
    turns each into a space so e.g. "S.W.A.T." or "Spider-Man" lines up
    with a filename's own already-spaced "S W A T" or "Spider Man". Both
    readings are kept since which one lines up depends on whether the
    *other* side spells the word with a space (needs the space reading) or
    runs it together (needs the deleted reading).

    Each of those readings is in turn compared both as-is, with every
    "a"/"an"/"the" dropped, and with any British spelling rewritten to its
    American equivalent (e.g. "encyclopaedia" reads the same as
    "encyclopedia") - in every combination - since Jellyfin metadata and
    filenames don't always agree on any of these (unlike
    :func:`normalized_title` itself, which leaves articles and spelling
    alone for callers that need an exact match to win over a
    same-titled-except-for-this coincidence).

    Each reading is also tried with a trailing standalone roman numeral
    rewritten as its arabic equivalent (e.g. "Chapter I" reads the same as
    "Chapter 1", and "Chapter XXV" the same as "Chapter 25") - unlike
    :func:`normalized_title`'s own roman-numeral handling, which only covers
    a parenthesized disambiguator or a "Part I" suffix, this also covers a
    title that's just a name plus a bare trailing numeral with no
    "Part"/parentheses at all (e.g. TheTVDB titling an episode "Chapter I"
    while Jellyfin's own metadata spells it "Chapter 1"). See
    :data:`_TRAILING_ROMAN_NUMERAL_PATTERN` for the tradeoff this generic
    match accepts.

    Each reading is separately tried with that same trailing standalone
    roman numeral dropped entirely instead of converted, treating it as a
    multi-part-episode disambiguator rather than significant title text -
    the same treatment :func:`normalized_title` already gives a purely
    numeric parenthetical like "(1)" or a "Part 1"/"Part I" suffix, just for
    the case where one side spells that same disambiguator as a bare
    trailing roman numeral with no parentheses or "Part" prefix at all (e.g.
    metadata titling a two-part episode's first half "Those Who Rend
    Asunder I" while the filename spells it "Those Who Rend Asunder (1)" -
    normalized_title() drops the numeric parenthetical on the filename side
    as a disambiguator, so the bare "I" needs the same treatment to still
    line up). Kept as an additional reading alongside the arabic-conversion
    one above rather than replacing it - a title where the numeral genuinely
    is the title text (e.g. "Chapter I" is its own distinct episode, not a
    disambiguated half of "Chapter") still needs that reading to line up
    with an arabic-spelled counterpart, so both stay available and either
    one matching counts.

    Finally, every resulting reading is also tried with all of its
    remaining spaces removed, so two words split apart on one side but
    joined into one on the other (e.g. "Doll House" versus "Dollhouse")
    still match - this is the least targeted of these readings (nothing
    else about the two sides needs to agree once whitespace is out of the
    picture), but it only ever adds a match alongside the word-for-word
    reading, never replaces it, so it can't turn two titles that actually
    differ into a false match on its own.

    Titles are a match if any combination of the two sides' readings
    agrees.

    Args:
        first: One title to compare, e.g. a filename- or stream-derived
            title.
        second: The other title to compare, e.g. a metadata title.

    Returns:
        ``True`` when the titles are equal under any reading.
    """
    return not _title_comparison_variants(first).isdisjoint(_title_comparison_variants(second))


def _title_comparison_variants(value: str) -> frozenset[str]:
    """Return every normalized reading of ``value`` that :func:`titles_match` allows."""
    raw_variants: set[str] = set()
    for comma_variant in (value, value.replace(",", "/")):
        raw_variants.add(comma_variant)
        raw_variants.add(_COMPOUND_PUNCTUATION_PATTERN.sub("", comma_variant))

    normalized_forms = {normalized_title(raw_variant) for raw_variant in raw_variants}
    normalized_forms |= {
        _with_trailing_roman_numeral_as_arabic(form) for form in normalized_forms
    }
    normalized_forms |= {
        _without_trailing_roman_numeral(form) for form in normalized_forms
    }
    variants: set[str] = set()
    for form in normalized_forms:
        for with_or_without_articles in (form, _without_articles(form)):
            variants.add(with_or_without_articles)
            variants.add(_with_american_spellings(with_or_without_articles))

    variants |= {variant.replace(" ", "") for variant in variants}
    return frozenset(variants)


def _with_trailing_roman_numeral_as_arabic(normalized_value: str) -> str:
    """Return ``normalized_value`` with a trailing standalone roman numeral rewritten as arabic.

    Any value converts, not just a small enumerated range (see
    :data:`_ROMAN_NUMERAL_SHAPE`) - a series like Star Wars: Clone Wars
    (2003) numbers episodes well past what a fixed list would cover
    ("Chapter I" through "Chapter XXV"). ``normalized_value`` is expected to
    already be casefolded and whitespace-collapsed, i.e. the output of
    :func:`normalized_title`.
    """
    match = _TRAILING_ROMAN_NUMERAL_PATTERN.search(normalized_value)
    if match is None:
        return normalized_value
    numeral_value = _roman_numeral_to_int(match.group(0))
    if numeral_value is None:
        return normalized_value
    return f"{normalized_value[:match.start()]}{numeral_value}"


def _without_trailing_roman_numeral(normalized_value: str) -> str:
    """Return ``normalized_value`` with a trailing standalone roman numeral dropped entirely.

    Treats the numeral as a multi-part-episode disambiguator rather than
    significant title text - see :func:`_title_comparison_variants` for why
    this reading is kept alongside, not instead of,
    :func:`_with_trailing_roman_numeral_as_arabic`. ``normalized_value`` is
    expected to already be casefolded and whitespace-collapsed, i.e. the
    output of :func:`normalized_title`.
    """
    match = _TRAILING_ROMAN_NUMERAL_PATTERN.search(normalized_value)
    if match is None:
        return normalized_value
    if _roman_numeral_to_int(match.group(0)) is None:
        return normalized_value
    return normalized_value[: match.start()].rstrip()


def _without_articles(normalized_value: str) -> str:
    """Return an already-:func:`normalized_title`-processed value with articles dropped."""
    without_articles = _ARTICLE_STOPWORD_PATTERN.sub("", normalized_value)
    return re.sub(r"\s+", " ", without_articles).strip()


def _with_american_spellings(normalized_value: str) -> str:
    """Return an already-:func:`normalized_title`-processed value with British spellings Americanized."""
    words = normalized_value.split(" ")
    return " ".join(_BRITISH_TO_AMERICAN_SPELLINGS.get(word, word) for word in words)


def _roman_numeral_paren_to_arabic(match: re.Match[str]) -> str:
    """Return an arabic-numeral parenthetical for a matched roman numeral."""
    numeral_value = _roman_numeral_to_int(match.group(1))
    if numeral_value is None:
        return match.group(0)
    return f"({numeral_value})"


def _roman_numeral_to_int(numeral: str) -> int | None:
    """Return the integer value of a roman numeral, or ``None`` when invalid."""
    total = 0
    previous_value = 0
    for character in reversed(numeral.upper()):
        value = _ROMAN_NUMERAL_VALUES.get(character)
        if value is None:
            return None
        if value < previous_value:
            total -= value
        else:
            total += value
            previous_value = value
    return total or None


def missing_tv_series_seasons(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Return findings for series with missing numbered seasons.

    Without TheTVDB data, a series' missing seasons can only be inferred from
    gaps between the lowest and highest season numbers present locally -
    there's no way to tell whether seasons are missing after the last one on
    disk. When ``aired_positions`` has an entry for a series, the set of
    season numbers found there is used instead, so seasons missing after the
    last local one (e.g. only seasons 1-2 exist locally but TheTVDB lists
    1-4) are caught too, not just internal gaps. Season 0 (specials) is
    never reported missing, even when TheTVDB lists specials absent locally
    - specials coverage on TheTVDB is inconsistent enough across series that
    a missing season 0 isn't a reliable signal of an actual gap.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number), as fetched for
            :func:`audit_episode_ordering`. When omitted, or when a series
            has no matching TVDB data, only internal gaps between
            locally-present season numbers are reported.

    Returns:
        One finding per TV series with missing numbered seasons.
    """
    series_items: dict[str, list[MediaItem]] = {}
    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.season_number < 0:
            continue
        series_items.setdefault(item.series_name, []).append(item)

    findings: list[AuditFinding] = []
    for series_name, grouped_items in sorted(series_items.items(), key=lambda entry: entry[0].casefold()):
        season_numbers = {item.season_number for item in grouped_items if item.season_number is not None}
        tvdb_season_numbers = _tvdb_series_season_numbers(aired_positions, series_name)
        missing_numbers = tuple(
            number for number in _missing_numbers(season_numbers, tvdb_season_numbers) if number != 0
        )
        if not missing_numbers:
            continue
        total_seasons = len({number for number in season_numbers if number != 0} | set(missing_numbers))
        representative = min(grouped_items, key=_episode_sort_key)
        findings.append(
            _finding(
                representative,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="missing_seasons",
                message=(
                    f"Missing seasons: {_format_missing_numbers(missing_numbers)}, "
                    f"out of {total_seasons} seasons."
                ),
            )
        )
    return tuple(findings)


def missing_tv_season_episodes(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Return findings for seasons with missing numbered episodes.

    Without TheTVDB data, a season's missing episodes can only be inferred
    from gaps between the lowest and highest episode numbers present locally
    - there's no way to tell whether episodes are missing after the last one
    on disk. When ``aired_positions`` has an entry for a series' season, its
    full TheTVDB episode list is used instead, so episodes missing after the
    last local one (e.g. only 1-8 exist locally but TheTVDB lists 1-10) are
    caught too, not just internal gaps.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number), as fetched for
            :func:`audit_episode_ordering`. When omitted, or when a series
            or season has no matching TVDB data, only internal gaps between
            locally-present episode numbers are reported.

    Returns:
        One finding per TV season with missing numbered episodes.
    """
    season_items: dict[tuple[str, int], list[MediaItem]] = {}
    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.season_number <= 0:
            continue
        if item.episode_number is None or item.episode_number <= 0:
            continue
        season_items.setdefault((item.series_name, item.season_number), []).append(item)

    findings: list[AuditFinding] = []
    for (series_name, season_number), grouped_items in sorted(
        season_items.items(),
        key=lambda entry: (entry[0][0].casefold(), entry[0][1]),
    ):
        episode_numbers = {
            item.episode_number for item in grouped_items if item.episode_number is not None
        }
        tvdb_episode_numbers = _tvdb_season_episode_numbers(aired_positions, series_name, season_number)
        missing_numbers = _missing_numbers(episode_numbers, tvdb_episode_numbers)
        if not missing_numbers:
            continue
        total_episodes = len(episode_numbers | set(missing_numbers))
        representative = min(grouped_items, key=_episode_sort_key)
        findings.append(
            _finding(
                representative,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="missing_episodes",
                message=(
                    f"Missing episodes: {_format_missing_numbers(missing_numbers)}, "
                    f"out of {total_episodes} episodes."
                ),
            )
        )
    return tuple(findings)


def _tvdb_season_episode_numbers(
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None,
    series_name: str,
    season_number: int,
) -> frozenset[int] | None:
    """Return TheTVDB's known episode numbers for one series' season, if any."""
    if not aired_positions:
        return None
    series_positions = aired_positions.get(series_name)
    if not series_positions:
        return None
    season_episode_numbers = frozenset(
        episode_number
        for position_season, episode_number in series_positions
        if position_season == season_number
    )
    return season_episode_numbers or None


def _tvdb_series_season_numbers(
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None,
    series_name: str,
) -> frozenset[int] | None:
    """Return TheTVDB's known season numbers for one series, if any."""
    if not aired_positions:
        return None
    series_positions = aired_positions.get(series_name)
    if not series_positions:
        return None
    series_season_numbers = frozenset(position_season for position_season, _ in series_positions)
    return series_season_numbers or None


def _missing_numbers(
    local_numbers: Iterable[int],
    tvdb_numbers: frozenset[int] | None,
) -> tuple[int, ...]:
    """Return missing numbers (season or episode) for one series or season.

    Without TheTVDB data (``tvdb_numbers`` is ``None``), only gaps between
    the lowest and highest locally-present numbers are reported. With
    TheTVDB data, every TVDB-listed number absent locally is reported,
    including ones after the last local number.
    """
    if tvdb_numbers is None:
        return _missing_sequence_numbers(local_numbers)
    return tuple(sorted(tvdb_numbers - set(local_numbers)))


def mismatched_tvdb_series(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
    dvd_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Return findings for series whose matched TheTVDB entry looks wrong.

    A series correctly matched to TheTVDB should have most of its local
    (season, episode) numbers land on a real TheTVDB position. When a series
    is matched to the wrong TheTVDB entry - e.g. a same-named but different
    show - most local episodes won't correspond to anything TheTVDB knows
    about at that position, since the two shows' season/episode numbering
    rarely lines up by coincidence. This is a different failure than a
    missing or mislabeled episode: it means the TheTVDB match itself, not
    any one episode, needs fixing (typically via Jellyfin's "Identify"
    dialog on that series).

    A local episode is considered matched when its (season, episode)
    position exists in either TheTVDB's aired order or its DVD order, since
    some series are numbered on disk in DVD order - checking aired order
    alone would otherwise flag those correctly-matched series as wrong.

    Only series with at least ``_MISMATCHED_TVDB_SERIES_MIN_EPISODES`` local
    episodes are considered, so a newly added series with only a couple of
    episodes on disk doesn't trigger a finding on thin evidence. Season 0
    (specials) is excluded, since specials numbering is often inconsistent
    across metadata sources even for a correctly matched series.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number). A series absent here
            (no TheTVDB match, or the lookup failed) is skipped - this check
            needs TheTVDB data to have something to compare against.
        dvd_positions: TheTVDB DVD-order episodes for each series name, in
            the same shape as ``aired_positions``. A local episode matching
            either ordering counts as matched.

    Returns:
        One finding per TV series whose local episodes mostly don't match
        TheTVDB's episode list.
    """
    if not aired_positions:
        return ()

    series_items = _local_numbered_episodes_by_series(items)

    findings: list[AuditFinding] = []
    for series_name, grouped_items in sorted(series_items.items(), key=lambda entry: entry[0].casefold()):
        series_aired_positions = aired_positions.get(series_name)
        if not series_aired_positions:
            continue
        if len(grouped_items) < _MISMATCHED_TVDB_SERIES_MIN_EPISODES:
            continue

        series_dvd_positions = dvd_positions.get(series_name) if dvd_positions else None
        unmatched_count, total_count = _unmatched_episode_count(
            grouped_items, series_aired_positions, series_dvd_positions
        )
        ratio = unmatched_count / total_count
        is_mismatched = ratio >= _MISMATCHED_TVDB_SERIES_MIN_UNMATCHED_RATIO
        if not is_mismatched:
            continue

        _log_mismatch_evaluation(
            series_name,
            grouped_items,
            series_aired_positions,
            series_dvd_positions,
            unmatched_count=unmatched_count,
            total_count=total_count,
            ratio=ratio,
        )

        representative = min(grouped_items, key=_episode_sort_key)
        findings.append(
            _finding(
                representative,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="mismatched_tvdb_series",
                message=(
                    f"{unmatched_count} of {total_count} local episodes don't match "
                    "any TheTVDB episode at their season/episode position - the matched TheTVDB "
                    "series may be wrong."
                ),
            )
        )
    return tuple(findings)


def _log_mismatch_evaluation(
    series_name: str,
    grouped_items: Iterable[MediaItem],
    series_aired_positions: Mapping[tuple[int, int], tuple[TvdbEpisode, ...]],
    series_dvd_positions: Mapping[tuple[int, int], tuple[TvdbEpisode, ...]] | None,
    *,
    unmatched_count: int,
    total_count: int,
    ratio: float,
) -> None:
    """Log one flagged series' mismatched_tvdb_series evaluation: per-episode matches and the score.

    Only called for a series that actually trips the mismatch threshold - a
    series that passes the check produces no log output, so this file stays
    a record of what to investigate rather than a full trace of every check
    run. Written to ``LOGGER`` at INFO, which only reaches disk when
    auditor.py has attached a file handler for it (``mismatched_tvdb_series.log``) -
    this is diagnostic detail for manually checking a specific finding, not
    something meant to appear on the console.
    """
    LOGGER.info(
        "Series %r: checking %d local episode(s) against %d TheTVDB aired-order and %d "
        "DVD-order position(s).",
        series_name,
        total_count,
        len(series_aired_positions),
        len(series_dvd_positions) if series_dvd_positions is not None else 0,
    )
    for item in sorted(grouped_items, key=_episode_sort_key):
        position = (item.season_number, item.episode_number)
        in_aired = position in series_aired_positions
        in_dvd = series_dvd_positions is not None and position in series_dvd_positions
        if in_aired and in_dvd:
            status = "matched (aired + dvd)"
        elif in_aired:
            status = "matched (aired)"
        elif in_dvd:
            status = "matched (dvd)"
        else:
            status = "unmatched"
        LOGGER.info(
            "  S%02dE%02d %r -> %s",
            item.season_number,
            item.episode_number,
            item.title,
            status,
        )
    LOGGER.info(
        "Series %r: score %d/%d unmatched (%.2f, threshold %.2f) -> MISMATCH FLAGGED",
        series_name,
        unmatched_count,
        total_count,
        ratio,
        _MISMATCHED_TVDB_SERIES_MIN_UNMATCHED_RATIO,
    )


def _local_numbered_episodes_by_series(items: Iterable[MediaItem]) -> dict[str, list[MediaItem]]:
    """Return each series' locally-present numbered episodes, excluding specials.

    Season 0 (specials) is excluded since specials numbering is often
    inconsistent across metadata sources even for a correctly matched
    series, which would otherwise add noise to any comparison against
    TheTVDB's episode list.
    """
    series_items: dict[str, list[MediaItem]] = {}
    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.season_number <= 0:
            continue
        if item.episode_number is None or item.episode_number <= 0:
            continue
        series_items.setdefault(item.series_name, []).append(item)
    return series_items


def _unmatched_episode_count(
    local_items: Iterable[MediaItem],
    series_positions: Mapping[tuple[int, int], object],
    secondary_series_positions: Mapping[tuple[int, int], object] | None = None,
) -> tuple[int, int]:
    """Return (unmatched_count, total_count) of local items against TheTVDB positions.

    An item counts as matched when its (season, episode) position is found
    in either ``series_positions`` or, when given, ``secondary_series_positions``
    - used to check a local episode against both TheTVDB's aired and DVD
    orderings. Only position membership matters, so the value type is
    irrelevant - callers pass a single :class:`TvdbEpisode` per position (one
    candidate series' own episode list) or a tuple of them (several
    same-named series merged into one position map) interchangeably.
    """
    local_items_tuple = tuple(local_items)
    unmatched_count = sum(
        1
        for item in local_items_tuple
        if (item.season_number, item.episode_number) not in series_positions
        and (
            secondary_series_positions is None
            or (item.season_number, item.episode_number) not in secondary_series_positions
        )
    )
    return unmatched_count, len(local_items_tuple)


def best_matching_tvdb_series(
    items: Iterable[MediaItem],
    series_name: str,
    candidates: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]],
) -> str | None:
    """Return the TheTVDB id among ``candidates`` that best fits one series' local episodes.

    Used to suggest a fix for a :func:`mismatched_tvdb_series` finding: given
    other same-named TheTVDB series found by name search, find one whose
    episode list actually explains the local files, so a wrong Jellyfin
    match can be pointed at the right TheTVDB entry instead of just being
    flagged as wrong.

    A candidate only qualifies as a confident match when at most
    ``_GOOD_TVDB_MATCH_MAX_UNMATCHED_RATIO`` of local episodes fail to
    correspond to one of its TheTVDB positions - a coincidental partial
    overlap isn't enough to recommend re-identifying a series. Among
    qualifying candidates, the one with the fewest unmatched episodes wins.

    Args:
        items: Media items from one audited library.
        series_name: The series to evaluate candidates for.
        candidates: Candidate TheTVDB series' aired-order episodes, keyed by
            TheTVDB id, each in the same ``(season_number, episode_number)``
            shape as :func:`mismatched_tvdb_series`'s ``aired_positions``.

    Returns:
        The best-fitting candidate's TheTVDB id, or ``None`` when no
        candidate is a confident match.
    """
    local_items = _local_numbered_episodes_by_series(items).get(series_name, [])
    if not local_items:
        return None

    best_id: str | None = None
    best_ratio = float("inf")
    for candidate_id, positions in candidates.items():
        unmatched_count, total_count = _unmatched_episode_count(local_items, positions)
        if total_count == 0:
            continue
        ratio = unmatched_count / total_count
        if ratio > _GOOD_TVDB_MATCH_MAX_UNMATCHED_RATIO:
            continue
        if ratio < best_ratio:
            best_ratio = ratio
            best_id = candidate_id
    return best_id


def identify_tvdb_series_ids(
    items: Iterable[MediaItem],
    series_name: str,
    assigned_ids: Iterable[str],
    aired_candidates: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]],
    dvd_candidates: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]] | None = None,
) -> tuple[str, ...]:
    """Return every TheTVDB id whose episode titles actually explain this series' local episodes.

    Used by :func:`auditor._fetch_tvdb_episode_positions` to decide which of
    a series' several same-named TheTVDB ids actually get their episode data
    merged into the position map local episodes are checked against. A
    series name can pick up more than one candidate id over time - not just
    TheTVDB genuinely splitting a long-running show into disjoint eras (e.g.
    a Jellyfin library holding two distinct Series items sharing one display
    name, one per era, each with its own assigned id - see
    :meth:`JellyfinClient.get_series_tvdb_ids`), but also an unrelated
    same-named show that TheTVDB's search once surfaced as a rejected
    candidate while looking for a better match for some other series
    entirely (see :func:`best_matching_tvdb_series`), whose episode list
    then stays recorded under the name in TheTVDB's episode cache
    indefinitely (see :meth:`TvdbClient.get_cached_series_ids_by_name`) - or
    even one Jellyfin itself mistakenly assigned, since its own automatic
    matching can be thrown off by weakly-titled local metadata just as
    easily as a human can.

    A candidate qualifies by actually explaining at least one local episode
    *title* - its own episode title at a local item's (season, episode)
    position, in either ordering, agreeing with that item's title (see
    :func:`titles_match`) - not merely by having *some* data at a shared
    position. Position overlap alone was tried first and reverted: an
    unrelated same-named show using an ordinary, similarly-sized
    season/episode grid can cover much of a real series' *position* space
    by pure numeric coincidence without its *content* having anything to do
    with it, which let position overlap alone pick - or merge in - the
    wrong candidate far too easily. Title-matching doesn't have that
    problem - two unrelated shows coincidentally sharing not just a
    (season, episode) position but the literal episode title there as well
    is vanishingly unlikely. Every qualifying candidate is returned, not
    just the single best one, so a genuine split across ids - including one
    whose local share happens to reuse (season, episode) numbers a
    dominant id also uses (e.g. a newer era locally renumbered back to
    "Season 1" instead of continuing the original's own numbering) - still
    has each side's real episode checked against its own id's title rather
    than only the dominant id's unrelated one at that position; see
    :func:`audit_episode_ordering`'s "matches any candidate" handling for
    where that pays off.

    When *no* candidate explains even a single local title - most commonly
    because every local title here is a placeholder Jellyfin never enriched
    with real episode metadata, leaving nothing for any candidate to
    actually agree with - there's no title evidence to work from at all, so
    this falls back to position overlap instead: the single candidate with
    the least of the series' full local (season, episode) set left
    unexplained by position alone (checked against both orderings, since
    some series are organized on disk in TheTVDB's DVD order while local
    numbers still follow aired order, or vice versa), tie-broken toward a
    Jellyfin-``assigned_ids`` candidate over one only present in TheTVDB's
    episode cache. Picking only one id in that fallback - rather than
    merging every position-overlapping candidate the way an earlier version
    of this function did - keeps a coincidental position-only collision
    from ever contaminating a position with an unrelated candidate's
    episode; with no title evidence available to resolve a collision
    correctly, :func:`audit_episode_ordering` flagging the position as not
    found in TheTVDB at all is the safer failure than a guess.

    Args:
        items: Media items from one audited library.
        series_name: The series to evaluate candidates for.
        assigned_ids: Ids Jellyfin has assigned to a Series item under this
            name - preferred on a tie in the no-title-evidence fallback.
        aired_candidates: Every candidate TheTVDB id's aired-order episodes,
            keyed by TheTVDB id, each in the same
            ``(season_number, episode_number)`` shape as
            :func:`mismatched_tvdb_series`'s ``aired_positions``.
        dvd_candidates: The same candidates' DVD-order episodes, in the same
            shape. A candidate id absent here is scored on its aired-order
            data alone.

    Returns:
        Every candidate id that explains at least one local episode title,
        or - only when none do - a single candidate id chosen by position
        overlap instead. Every candidate id unchanged when there's no local
        numbered-episode data to score candidates against at all.
    """
    if not aired_candidates:
        return ()
    if len(aired_candidates) == 1:
        return tuple(aired_candidates)

    local_items = _local_numbered_episodes_by_series(items).get(series_name, [])
    if not local_items:
        return tuple(aired_candidates)

    title_matched_ids: list[str] = []
    ratio_by_id: dict[str, float] = {}
    for candidate_id, positions in aired_candidates.items():
        secondary_positions = (dvd_candidates or {}).get(candidate_id, {})
        matched_title_count = sum(
            1
            for item in local_items
            if _candidate_title_matches(positions, secondary_positions, item)
        )
        if matched_title_count > 0:
            title_matched_ids.append(candidate_id)
            continue
        unmatched_count, total_count = _unmatched_episode_count(
            local_items, positions, secondary_positions
        )
        ratio_by_id[candidate_id] = unmatched_count / total_count if total_count else 1.0

    if title_matched_ids:
        return tuple(title_matched_ids)

    assigned = frozenset(assigned_ids)
    best_id = min(ratio_by_id, key=lambda cid: (ratio_by_id[cid], 0 if cid in assigned else 1))
    return (best_id,)


def _candidate_title_matches(
    positions: Mapping[tuple[int, int], TvdbEpisode],
    secondary_positions: Mapping[tuple[int, int], TvdbEpisode],
    item: MediaItem,
) -> bool:
    """Return whether a candidate has an episode at one item's position whose title matches it.

    Checks ``positions`` (aired order) first, falling back to
    ``secondary_positions`` (DVD order) only when aired has nothing there -
    either ordering agreeing with the local title is enough to count as a
    match for :func:`identify_tvdb_series_ids`'s scoring.
    """
    position = (item.season_number, item.episode_number)
    episode = positions.get(position) or secondary_positions.get(position)
    return episode is not None and titles_match(item.title, episode.name)


def mismatched_tvdb_title(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
    dvd_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Return findings for local episodes whose title doesn't match TheTVDB's cached aired-order title.

    Primarily compares against aired order - the ordering ``tvdb_cache.json``
    and TheTVDB's own default reflect - and runs unconditionally whenever a
    TheTVDB ``api_key`` is configured, the same as :func:`mismatched_tvdb_series`,
    not just with ``--check-episode-order``. Unlike :func:`audit_episode_ordering`,
    a mismatch against aired order is enough to flag on its own - DVD-order
    data isn't required to be available first.

    However, a local title matching DVD order instead of aired order at that
    same position is not flagged: a series correctly organized end-to-end in
    DVD order is expected to disagree with aired order at every episode, and
    without this check, every one of its episodes would otherwise read as a
    false "mismatch". So DVD order is only ever used to *excuse* a title that
    disagrees with aired order, never to flag one that agrees with aired
    order - unlike :func:`audit_episode_ordering`, which requires disagreement
    with both before flagging anything.

    Shares :func:`audit_episode_ordering`'s multi-episode-range and
    multi-candidate-series handling (see its docstring for why both matter):
    a file spanning more than one episode is compared against every
    position's title joined together, and a series name matching more than
    one TheTVDB id is checked against the union of all of their episodes,
    with a candidate still in its original, non-English language ignored
    (see :data:`_NON_ENGLISH_SCRIPT_PATTERN`).

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order candidate episodes for each
            series name, keyed by (season_number, episode_number). A series
            absent here (no TheTVDB match, or the lookup failed) is skipped.
        dvd_positions: TheTVDB DVD-order candidate episodes for each series
            name, in the same shape as ``aired_positions``. Used only to
            excuse a title that disagrees with aired order but agrees with
            DVD order instead; absent or missing-position data just means
            there's no DVD-order title available to excuse it with.

    Returns:
        One finding per local episode (or multi-episode range) whose title
        doesn't match any English-titled candidate combination's TheTVDB
        aired-order title at its (season, episode(s)) position, and also
        doesn't match its DVD-order title there. Also includes a separate,
        informational finding (check_name "tvdb_title_not_english") for an
        episode whose aired-order position has TheTVDB data at every
        position it covers, but none of it in English - see
        :func:`_has_non_english_only_candidates` for why that's neither a
        real comparison nor the total absence of one.
    """
    if not aired_positions:
        return ()

    findings: list[AuditFinding] = []

    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.episode_number is None:
            continue

        episode_numbers = expected_episode_numbers_from_filename(item) or (item.episode_number,)
        if len(episode_numbers) > 1:
            position_label = (
                f"S{item.season_number:02d}E{episode_numbers[0]:02d}-"
                f"E{episode_numbers[-1]:02d}"
            )
        else:
            position_label = f"S{item.season_number:02d}E{item.episode_number:02d}"

        aired_series_positions = aired_positions.get(item.series_name, {})
        aired_per_position = _candidates_for_episode_range(
            aired_series_positions, item.season_number, episode_numbers
        )
        if aired_per_position is None:
            if _has_non_english_only_candidates(
                aired_series_positions, item.season_number, episode_numbers
            ):
                findings.append(
                    _finding(
                        item,
                        category=AuditCategory.METADATA,
                        severity=AuditSeverity.INFO,
                        check_name="tvdb_title_not_english",
                        message=(
                            f"{position_label} has TheTVDB aired-order data, but no English "
                            "translation is on file for it, so its title can't be compared."
                        ),
                    )
                )
            continue

        aired_combined_titles = _combined_candidate_titles(aired_per_position)
        if any(titles_match(item.title, combined) for combined in aired_combined_titles):
            continue

        if dvd_positions:
            dvd_per_position = _candidates_for_episode_range(
                dvd_positions.get(item.series_name, {}), item.season_number, episode_numbers
            )
            if dvd_per_position is not None:
                dvd_combined_titles = _combined_candidate_titles(dvd_per_position)
                if any(titles_match(item.title, combined) for combined in dvd_combined_titles):
                    continue

        findings.append(
            _finding(
                item,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="mismatched_tvdb_title",
                message=(
                    f'{position_label} is titled "{item.title}", but TheTVDB\'s cached '
                    f"aired-order title at that position is "
                    f"{_format_combined_titles(aired_combined_titles)}."
                ),
            )
        )

    return tuple(findings)


def audit_episode_ordering(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]],
    dvd_positions: Mapping[str, Mapping[tuple[int, int], tuple[TvdbEpisode, ...]]],
) -> tuple[AuditFinding, ...]:
    """Return findings for local episodes whose title doesn't match TheTVDB's aired-order title.

    Some series are organized on disk in TheTVDB's DVD order while Jellyfin's
    season/episode numbers still follow aired order (or vice versa), so the
    filename and season/episode numbers all look correct even though the
    video content is a different episode. This compares each local episode's
    own metadata title against TheTVDB's aired-order title at that
    (season, episode) position.

    A single video file can span more than one episode, e.g.
    ``Show S01E05-E07 Title.mkv`` (see
    :func:`media.expected_episode_numbers_from_filename`) - Jellyfin's own
    ``IndexNumber`` for such an item is just the range's first episode (5
    here), so comparing only that one position's title against the item's
    combined metadata title would be comparing a single episode's title
    against three episodes' worth of text and calling the difference a
    mismatch. When the filename implies a range, every position in it (5, 6,
    and 7) is required to have data, and each ordering's expected title is
    every one of those positions' titles joined with "/", mirroring
    Jellyfin's own convention for combining a multi-episode item's title
    (see :func:`titles_match`, which already treats "/" and "," as
    equivalent joiners) - a local title matching that joined title counts as
    a match for the whole range. An ordinary single-episode file is just the
    ``len(episode_numbers) == 1`` case of the same logic, unchanged.

    A name can carry more than one candidate episode at the same position -
    e.g. TheTVDB splitting a franchise into more than one series entry, each
    independently numbering its own "Season 1, Episode 1" (see
    :func:`auditor._fetch_tvdb_episode_positions`). Combined with a
    multi-episode range, this means there can be more than one way to join
    a range's positions into an expected title (one per candidate at each
    position) - every combination is generated, and a local title matching
    any one of them counts as a match; only when it matches none of them,
    for both orderings, is it flagged - and the message lists every
    distinct combined title actually on offer, since there's no single
    "the" aired-order title to quote once more than one combination exists.

    A mismatch is only reported when DVD-order data is available for that
    position too - without it there's no second ordering to confirm a real
    discrepancy against, only that the local title differs from one
    ordering's, which alone isn't unusual (typos, alternate titles, etc.).
    A local title matching DVD order instead of aired order is not reported
    at all - a series correctly organized end-to-end in DVD order would
    otherwise disagree with aired order, and so get "flagged", at every
    single episode. Only a local title matching neither ordering is a
    genuine discrepancy worth flagging.

    A candidate whose title is still in its original, non-English language
    (see :data:`_NON_ENGLISH_SCRIPT_PATTERN`) is ignored entirely at a
    position, same as if it weren't there at all - there's no crowd-sourced
    English translation on file for it, so there's no way to tell whether it
    actually matches the local title or not, and reporting it as a mismatch
    on the strength of a comparison that can't mean anything would itself be
    the false positive.

    A series name present in ``aired_positions`` at all (even as an empty
    position map) means TheTVDB series id it's been identified against (see
    :func:`auditor._fetch_tvdb_episode_positions`) was actually looked up -
    a local (season, episode) that id has no data for in *either* ordering
    is flagged too, as its own ``episode_not_in_tvdb`` finding, rather than
    silently skipped, so a season TheTVDB genuinely doesn't have for the
    identified series (e.g. a newer era TheTVDB tracks as an entirely
    separate series, or a season that simply isn't out yet) is surfaced as
    exactly that, instead of staying invisible or - worse - getting compared
    against a same-named but unrelated series' totally different episode at
    that position. Kept a distinct check name from ``aired_dvd_order_mismatch``
    so CSV/XLSX output can tell "not found at all" apart from "found, and
    disagrees" (see :func:`reports.generator._csv_rows`'s "NF" designation),
    even though :func:`reports.generator._actionable_findings` folds it into
    the same HTML "Aired/DVD Order Mismatch" page a viewer is already
    looking at. A series name absent from ``aired_positions`` entirely (no
    id was ever identified for it) is skipped as before - there's nothing to
    say a specific season is missing from, only that nothing was looked up
    at all.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order candidate episodes for each
            series name, keyed by (season_number, episode_number). A series
            name's presence as a key - regardless of whether its value is
            empty - marks that series as having an identified TheTVDB id.
        dvd_positions: TheTVDB DVD-order candidate episodes for each series
            name, keyed by (season_number, episode_number).

    Returns:
        One ``aired_dvd_order_mismatch`` finding per local episode (or
        multi-episode range) whose title doesn't match any English-titled
        candidate combination's TheTVDB title at its (season, episode(s))
        position, in either ordering, plus one ``episode_not_in_tvdb``
        finding per local episode whose identified series has no data at
        all - in either ordering - at its position.
    """
    findings: list[AuditFinding] = []

    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.episode_number is None:
            continue
        if item.series_name not in aired_positions:
            continue

        episode_numbers = expected_episode_numbers_from_filename(item) or (item.episode_number,)
        if len(episode_numbers) > 1:
            position_label = (
                f"S{item.season_number:02d}E{episode_numbers[0]:02d}-"
                f"E{episode_numbers[-1]:02d}"
            )
        else:
            position_label = f"S{item.season_number:02d}E{item.episode_number:02d}"

        aired_series_positions = aired_positions[item.series_name]
        dvd_series_positions = dvd_positions.get(item.series_name, {})

        if _range_entirely_missing(
            aired_series_positions, item.season_number, episode_numbers
        ) and _range_entirely_missing(dvd_series_positions, item.season_number, episode_numbers):
            findings.append(
                _finding(
                    item,
                    category=AuditCategory.EPISODE_ORDER,
                    severity=AuditSeverity.WARNING,
                    check_name="episode_not_in_tvdb",
                    message=(
                        f'{position_label} is titled "{item.title}", but that season/episode '
                        "was not found in TheTVDB at all, in either ordering, for the TheTVDB "
                        f'series identified for "{item.series_name}". Either this content '
                        "belongs to a different TheTVDB series, or TheTVDB doesn't have this "
                        "season yet."
                    ),
                )
            )
            continue

        aired_per_position = _candidates_for_episode_range(
            aired_series_positions, item.season_number, episode_numbers
        )
        if aired_per_position is None:
            continue

        aired_combined_titles = _combined_candidate_titles(aired_per_position)
        if any(titles_match(item.title, combined) for combined in aired_combined_titles):
            continue

        dvd_per_position = _candidates_for_episode_range(
            dvd_series_positions, item.season_number, episode_numbers
        )
        if dvd_per_position is None:
            continue

        dvd_combined_titles = _combined_candidate_titles(dvd_per_position)
        if any(titles_match(item.title, combined) for combined in dvd_combined_titles):
            continue

        message = (
            f'{position_label} is titled "{item.title}", which matches neither TheTVDB\'s '
            f"aired-order title {_format_combined_titles(aired_combined_titles)} nor its "
            f"DVD-order title {_format_combined_titles(dvd_combined_titles)} at that position. "
            "Verify the video content before trusting Jellyfin's metadata."
        )

        findings.append(
            _finding(
                item,
                category=AuditCategory.EPISODE_ORDER,
                severity=AuditSeverity.WARNING,
                check_name="aired_dvd_order_mismatch",
                message=message,
            )
        )

    return tuple(findings)


def is_untranslated_tvdb_title(title: str) -> bool:
    """Return whether a TheTVDB title is still in its original, non-English script.

    See :data:`_NON_ENGLISH_SCRIPT_PATTERN` - TheTVDB silently falls back to
    a series' original-language name for any episode with no English
    translation on file, with no separate flag in the API response saying
    that happened. Public so apply_tvdb_metadata.py
    can refuse to rename a Jellyfin item's title to text in a script its
    metadata almost certainly isn't otherwise written in - the same
    protection :func:`mismatched_tvdb_title` already gives comparisons via
    :func:`_english_titled_candidates` below.
    """
    return bool(_NON_ENGLISH_SCRIPT_PATTERN.search(title))


def _english_titled_candidates(
    candidates: tuple[TvdbEpisode, ...] | None,
) -> tuple[TvdbEpisode, ...]:
    """Return only the candidates whose title isn't still in a non-English script.

    See :data:`_NON_ENGLISH_SCRIPT_PATTERN`.
    """
    if not candidates:
        return ()
    return tuple(candidate for candidate in candidates if not is_untranslated_tvdb_title(candidate.name))


def _candidates_for_episode_range(
    positions: Mapping[tuple[int, int], tuple[TvdbEpisode, ...]],
    season_number: int,
    episode_numbers: tuple[int, ...],
) -> tuple[tuple[TvdbEpisode, ...], ...] | None:
    """Return each position's English-titled candidates across a multi-episode range.

    Returns ``None`` when any position in the range has no data at all (no
    candidates, or only non-English ones) - a combined title can't be
    confidently built with one of the range's episodes missing, and treating
    a partial range as if it fully matched (or fully mismatched) would be
    guessing.
    """
    per_position_candidates: list[tuple[TvdbEpisode, ...]] = []
    for episode_number in episode_numbers:
        candidates = _english_titled_candidates(positions.get((season_number, episode_number)))
        if not candidates:
            return None
        per_position_candidates.append(candidates)
    return tuple(per_position_candidates)


def _range_entirely_missing(
    positions: Mapping[tuple[int, int], tuple[TvdbEpisode, ...]],
    season_number: int,
    episode_numbers: tuple[int, ...],
) -> bool:
    """Return whether every position in a range has no TheTVDB data at all.

    Unlike :func:`_candidates_for_episode_range`'s ``None`` result, this
    checks the raw, unfiltered position data - so a position with only a
    non-English-titled candidate (or a partial range, where only some
    positions have data) does not count as missing here. Used by
    :func:`audit_episode_ordering` to tell "this position genuinely isn't in
    the identified TheTVDB series at all" apart from those other two cases,
    which stay silent instead (see :func:`_candidates_for_episode_range`'s
    docstring for why guessing on a partial range would be wrong, and
    :func:`_has_non_english_only_candidates` for the non-English case).
    """
    return all(
        not positions.get((season_number, episode_number)) for episode_number in episode_numbers
    )


def _has_non_english_only_candidates(
    positions: Mapping[tuple[int, int], tuple[TvdbEpisode, ...]],
    season_number: int,
    episode_numbers: tuple[int, ...],
) -> bool:
    """Return whether every position in the range has TheTVDB data, with at least one English-blocked.

    Distinguishes "TheTVDB has an episode here, but only in its original,
    untranslated language, at one or more positions" (this) from "TheTVDB
    has nothing at all at some position" (``False`` - the ordinary, silent
    skip a missing position already gets). A caller that's already handling
    a ``_candidates_for_episode_range()`` ``None`` result calls this to tell
    the two apart, since both cases return ``None`` there but only this one
    is worth reporting as a "no English title available" outcome rather
    than treating a missing title comparison as if it simply hadn't come up
    at all. For a multi-episode range, only one position needs to be
    English-blocked to make the whole combined comparison impossible - the
    other positions having English data doesn't help build a title that's
    missing one of its parts.
    """
    saw_english_blocked_position = False
    for episode_number in episode_numbers:
        raw_candidates = positions.get((season_number, episode_number))
        if not raw_candidates:
            return False
        if not _english_titled_candidates(raw_candidates):
            saw_english_blocked_position = True
    return saw_english_blocked_position


def _combined_candidate_titles(
    per_position_candidates: tuple[tuple[TvdbEpisode, ...], ...],
) -> tuple[str, ...]:
    """Return every way of joining one candidate title per position into one combined title.

    More than one position (a multi-episode range) or more than one
    candidate at a position (see :func:`audit_episode_ordering`) each
    multiply the number of ways the range's expected title could read -
    every combination is generated here so the caller can treat a match
    against any one of them as a match for the whole range.
    """
    return tuple(
        " / ".join(candidate.name for candidate in combination)
        for combination in itertools.product(*per_position_candidates)
    )


def _format_combined_titles(combined_titles: Iterable[str]) -> str:
    """Return every distinct combined title, quoted and joined for a finding message.

    More than one combined title (see :func:`_combined_candidate_titles`)
    means there's no single title to quote, so every distinct one is listed.
    """
    distinct_titles = dict.fromkeys(combined_titles)
    return " or ".join(f'"{title}"' for title in distinct_titles)


def _missing_sequence_numbers(numbers: Iterable[int]) -> tuple[int, ...]:
    """Return missing integers between the smallest and largest values."""
    sorted_numbers = sorted(set(numbers))
    if len(sorted_numbers) < 2:
        return ()

    missing_numbers: list[int] = []
    for previous, current in zip(sorted_numbers, sorted_numbers[1:]):
        if current - previous <= 1:
            continue
        missing_numbers.extend(range(previous + 1, current))
    return tuple(missing_numbers)


def _format_missing_numbers(numbers: Iterable[int]) -> str:
    """Return a compact string for missing number sequences."""
    sorted_numbers = sorted(set(numbers))
    if not sorted_numbers:
        return ""

    ranges: list[str] = []
    range_start = sorted_numbers[0]
    range_end = sorted_numbers[0]

    for number in sorted_numbers[1:]:
        if number == range_end + 1:
            range_end = number
            continue
        ranges.append(_format_number_range(range_start, range_end))
        range_start = number
        range_end = number

    ranges.append(_format_number_range(range_start, range_end))
    return ", ".join(ranges)


def _format_number_range(start: int, end: int) -> str:
    """Return one display range for missing season or episode numbers."""
    if start == end:
        return str(start)
    return f"{start}-{end}"


_MISSING_NUMBERS_MESSAGE_PATTERN = re.compile(
    r"^Missing (?:seasons|episodes): (.+), out of \d+ (?:seasons|episodes)\.$"
)


def missing_number_count(message: str) -> int | None:
    """Return how many individual seasons/episodes a missing_seasons/missing_episodes message names.

    The inverse of :func:`_format_missing_numbers`: e.g. "Missing seasons:
    2, 4-6, out of 8 seasons." names 4 individual seasons missing (2, 4, 5,
    and 6), not the 2 comma-separated segments the message text has - a
    range collapses to one segment in the message, but each number in it
    still counts on its own here. Used by xlsx_report.py's series summary
    sheet, which only has each finding's ``message`` to work with (not the
    original ``missing_numbers`` set :func:`missing_tv_series_seasons`/
    :func:`missing_tv_season_episodes` computed it from), since
    :class:`audit_types.AuditFinding` has no separate structured field for
    it.

    Args:
        message: A finding's ``message``, expected to be one
            ``missing_tv_series_seasons``/``missing_tv_season_episodes``
            produced (i.e. its ``check_name`` is "missing_seasons" or
            "missing_episodes") - the shape this parses is specific to
            those two.

    Returns:
        The count, or ``None`` when ``message`` doesn't match that exact
        shape at all (defensive - a caller should never see this for any
        other check's finding).
    """
    match = _MISSING_NUMBERS_MESSAGE_PATTERN.match(message)
    if match is None:
        return None
    return sum(_count_number_range(segment) for segment in match.group(1).split(", "))


def _count_number_range(segment: str) -> int:
    """Return how many individual numbers one :func:`_format_missing_numbers` segment represents."""
    if "-" not in segment:
        return 1
    start_text, end_text = segment.split("-", 1)
    return int(end_text) - int(start_text) + 1


def _episode_sort_key(item: MediaItem) -> tuple[str, int, int, str]:
    """Return a stable sort key for episode representative selection."""
    return (
        (item.series_name or "").casefold(),
        item.season_number if item.season_number is not None else -1,
        item.episode_number if item.episode_number is not None else -1,
        item.title.casefold(),
    )


def _finding(
    item: MediaItem,
    *,
    category: AuditCategory,
    severity: AuditSeverity,
    check_name: str,
    message: str,
) -> AuditFinding:
    """Build an audit finding for a media item.

    Args:
        item: Media item associated with the finding.
        category: Finding category.
        severity: Finding severity.
        check_name: Stable audit check name.
        message: Human-readable description.

    Returns:
        A structured audit finding.
    """
    return AuditFinding(
        category=category,
        severity=severity,
        check_name=check_name,
        message=message,
        media_item=item,
    )


__all__ = [
    "AuditCategory",
    "AuditFinding",
    "AuditSeverity",
    "audit_episode_ordering",
    "audit_library_items",
    "audit_media_item",
    "best_matching_tvdb_series",
    "identify_tvdb_series_ids",
    "is_untranslated_tvdb_title",
    "mismatched_episode_filename_title",
    "mismatched_movie_filename_title",
    "mismatched_tvdb_series",
    "mismatched_tvdb_title",
    "missing_backdrop",
    "missing_english_subtitles",
    "missing_episode_number",
    "missing_number_count",
    "missing_tv_season_episodes",
    "missing_tv_series_seasons",
    "missing_primary_image",
    "normalized_title",
    "titles_match",
    "unknown_audio_codec",
    "unknown_video_codec",
]
