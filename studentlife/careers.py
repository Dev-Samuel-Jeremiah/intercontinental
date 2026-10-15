"""
studentlife/careers.py
──────────────────────
A short interest quiz, a list of popular Nigerian university courses with the
UTME (JAMB) subject combinations they usually ask for, and a checker that
compares a course with the subjects a student offers (F287, F288).

Combinations change and differ between universities. Always confirm in the
current JAMB brochure — the page says so to students.
"""
import re

CLUSTERS = {
    'HEALTH': 'Health and medicine',
    'SCIENCE': 'Science and research',
    'TECH': 'Engineering and technology',
    'BUSINESS': 'Business and finance',
    'LAW': 'Law, government and society',
    'MEDIA': 'Arts, media and design',
    'EARTH': 'Agriculture and the environment',
    'CARE': 'Teaching and helping people',
}

QUIZ = [
    ('I like finding out how the human body works.', 'HEALTH'),
    ('I would enjoy caring for sick or injured people.', 'HEALTH'),
    ('I enjoy experiments in the laboratory.', 'SCIENCE'),
    ('I like solving hard maths problems.', 'TECH'),
    ('I like building, fixing or taking things apart.', 'TECH'),
    ('I enjoy using computers to make things, not just to play.', 'TECH'),
    ('I like buying, selling or managing money.', 'BUSINESS'),
    ('I would like to run my own business one day.', 'BUSINESS'),
    ('I enjoy debating and arguing a case.', 'LAW'),
    ('I care about how the country is governed.', 'LAW'),
    ('I like writing stories, drawing, music or acting.', 'MEDIA'),
    ('I would enjoy presenting on radio, TV or social media.', 'MEDIA'),
    ('I like plants, animals and working outdoors.', 'EARTH'),
    ('I worry about the environment and climate.', 'EARTH'),
    ('I like explaining things to younger pupils.', 'CARE'),
    ('People come to me when they need advice.', 'CARE'),
]

ENG, MATHS, PHY, CHEM, BIO = 'English Language', 'Mathematics', 'Physics', 'Chemistry', 'Biology'
ECONS, GOVT, LIT, GEOG, AGRIC = 'Economics', 'Government', 'Literature in English', 'Geography', 'Agricultural Science'
CRS, IRS, HIST, ART, COMM = 'Christian Religious Studies', 'Islamic Studies', 'History', 'Fine Art', 'Commerce'
ACCT = 'Financial Accounting'
ARTS_SOCIAL = [GOVT, LIT, CRS, IRS, HIST, ECONS, GEOG, COMM, ACCT]

COURSES = [
    {'name': 'Medicine and Surgery', 'cluster': 'HEALTH', 'required': [ENG, BIO, CHEM, PHY], 'one_of': []},
    {'name': 'Pharmacy', 'cluster': 'HEALTH', 'required': [ENG, BIO, CHEM, PHY], 'one_of': []},
    {'name': 'Nursing Science', 'cluster': 'HEALTH', 'required': [ENG, BIO, CHEM, PHY], 'one_of': []},
    {'name': 'Medical Laboratory Science', 'cluster': 'HEALTH', 'required': [ENG, BIO, CHEM, PHY], 'one_of': []},
    {'name': 'Veterinary Medicine', 'cluster': 'EARTH', 'required': [ENG, BIO, CHEM, PHY], 'one_of': []},
    {'name': 'Microbiology', 'cluster': 'SCIENCE', 'required': [ENG, BIO, CHEM], 'one_of': [[PHY, MATHS]]},
    {'name': 'Biochemistry', 'cluster': 'SCIENCE', 'required': [ENG, BIO, CHEM], 'one_of': [[PHY, MATHS]]},
    {'name': 'Geology', 'cluster': 'SCIENCE', 'required': [ENG, CHEM, PHY], 'one_of': [[MATHS, GEOG, BIO]]},
    {'name': 'Civil Engineering', 'cluster': 'TECH', 'required': [ENG, MATHS, PHY, CHEM], 'one_of': []},
    {'name': 'Electrical/Electronic Engineering', 'cluster': 'TECH', 'required': [ENG, MATHS, PHY, CHEM],
     'one_of': []},
    {'name': 'Mechanical Engineering', 'cluster': 'TECH', 'required': [ENG, MATHS, PHY, CHEM], 'one_of': []},
    {'name': 'Computer Science', 'cluster': 'TECH', 'required': [ENG, MATHS, PHY],
     'one_of': [[CHEM, BIO, AGRIC, ECONS, GEOG]]},
    {'name': 'Architecture', 'cluster': 'TECH', 'required': [ENG, MATHS, PHY],
     'one_of': [[CHEM, GEOG, ART, BIO, ECONS]]},
    {'name': 'Accounting', 'cluster': 'BUSINESS', 'required': [ENG, MATHS, ECONS], 'one_of': [ARTS_SOCIAL]},
    {'name': 'Economics', 'cluster': 'BUSINESS', 'required': [ENG, MATHS, ECONS], 'one_of': [ARTS_SOCIAL]},
    {'name': 'Business Administration', 'cluster': 'BUSINESS', 'required': [ENG, MATHS, ECONS],
     'one_of': [ARTS_SOCIAL]},
    {'name': 'Banking and Finance', 'cluster': 'BUSINESS', 'required': [ENG, MATHS, ECONS], 'one_of': [ARTS_SOCIAL]},
    {'name': 'Law', 'cluster': 'LAW', 'required': [ENG, LIT], 'one_of': [ARTS_SOCIAL, ARTS_SOCIAL]},
    {'name': 'Political Science', 'cluster': 'LAW', 'required': [ENG, GOVT], 'one_of': [ARTS_SOCIAL, ARTS_SOCIAL]},
    {'name': 'International Relations', 'cluster': 'LAW', 'required': [ENG, GOVT],
     'one_of': [ARTS_SOCIAL, ARTS_SOCIAL]},
    {'name': 'Mass Communication', 'cluster': 'MEDIA', 'required': [ENG, LIT], 'one_of': [ARTS_SOCIAL, ARTS_SOCIAL]},
    {'name': 'Theatre Arts', 'cluster': 'MEDIA', 'required': [ENG, LIT], 'one_of': [ARTS_SOCIAL, ARTS_SOCIAL]},
    {'name': 'Fine and Applied Arts', 'cluster': 'MEDIA', 'required': [ENG, ART], 'one_of': [ARTS_SOCIAL, ARTS_SOCIAL]},
    {'name': 'Agriculture', 'cluster': 'EARTH', 'required': [ENG, CHEM], 'one_of': [[BIO, AGRIC], [PHY, MATHS]]},
    {'name': 'Environmental Management', 'cluster': 'EARTH', 'required': [ENG, GEOG],
     'one_of': [[BIO, CHEM, ECONS], [MATHS, PHY, AGRIC]]},
    {'name': 'Education (sciences)', 'cluster': 'CARE', 'required': [ENG],
     'one_of': [[BIO, CHEM, PHY, MATHS], [BIO, CHEM, PHY, MATHS], [BIO, CHEM, PHY, MATHS]]},
    {'name': 'Education (arts)', 'cluster': 'CARE', 'required': [ENG], 'one_of': [ARTS_SOCIAL, ARTS_SOCIAL,
                                                                                 ARTS_SOCIAL]},
    {'name': 'Psychology', 'cluster': 'CARE', 'required': [ENG, BIO], 'one_of': [ARTS_SOCIAL + [CHEM, MATHS],
                                                                                 ARTS_SOCIAL + [CHEM, MATHS]]},
    {'name': 'Social Work', 'cluster': 'CARE', 'required': [ENG], 'one_of': [ARTS_SOCIAL, ARTS_SOCIAL, ARTS_SOCIAL]},
]

ALIASES = {
    'english': ENG, 'englishlanguage': ENG, 'useofenglish': ENG,
    'maths': MATHS, 'math': MATHS, 'mathematics': MATHS, 'generalmathematics': MATHS,
    'physics': PHY, 'chemistry': CHEM, 'biology': BIO,
    'economics': ECONS, 'econs': ECONS, 'government': GOVT, 'civiceducation': None,
    'literature': LIT, 'literatureinenglish': LIT, 'lit': LIT,
    'geography': GEOG, 'agric': AGRIC, 'agriculture': AGRIC, 'agriculturalscience': AGRIC,
    'crs': CRS, 'crk': CRS, 'christianreligiousstudies': CRS, 'christianreligiousknowledge': CRS,
    'irs': IRS, 'irk': IRS, 'islamicstudies': IRS, 'islamicreligiousstudies': IRS,
    'history': HIST, 'fineart': ART, 'fineartsart': ART, 'visualart': ART, 'art': ART, 'fineandappliedart': ART,
    'commerce': COMM, 'financialaccounting': ACCT, 'accounting': ACCT, 'accounts': ACCT,
}


def canonical(name):
    key = re.sub(r'[^a-z]', '', (name or '').lower())
    return ALIASES.get(key, (name or '').strip())


def course(name):
    return next((c for c in COURSES if c['name'] == name), None)


def check(course_row, subject_names):
    """{'ok', 'have', 'missing', 'choose'} for a course against subjects a student offers."""
    have = {canonical(s) for s in subject_names} - {None}
    missing = [s for s in course_row['required'] if s not in have]
    used = set(course_row['required'])
    choose = []
    for group in course_row['one_of']:
        pick = next((s for s in group if s in have and s not in used), None)
        if pick:
            used.add(pick)
        else:
            choose.append(sorted(set(group) - used))
    return {'ok': not missing and not choose, 'have': sorted(used & have), 'missing': missing, 'choose': choose}


def score_quiz(answers):
    """answers: {index: 1..3}. Returns clusters ordered best first with scores."""
    totals = {k: 0 for k in CLUSTERS}
    for i, (_text, cluster) in enumerate(QUIZ):
        try:
            totals[cluster] += max(0, min(3, int(answers.get(str(i), 0))))
        except (TypeError, ValueError):
            pass
    return sorted(totals.items(), key=lambda kv: -kv[1])


def suggestions(clusters, subject_names, limit=8):
    """Courses in the student's top interest areas, those their subjects already fit first."""
    top = [c for c, score in clusters[:3] if score > 0]
    rows = []
    for c in COURSES:
        if c['cluster'] in top:
            res = check(c, subject_names)
            rows.append({'course': c, 'cluster': CLUSTERS[c['cluster']], **res, 'rank': top.index(c['cluster'])})
    rows.sort(key=lambda r: (not r['ok'], r['rank'], len(r['missing'])))
    return rows[:limit]
