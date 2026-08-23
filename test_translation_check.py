"""Tests for the translation check: the deterministic year guard, and what
translate_checked does with a verdict.

Run from this directory:

    python3 -m unittest              # or: python3 test_translation_check.py

Stdlib only. No network and no model call: check_translation is stubbed, so
what is tested here is the part that decides — retry, drop the description,
drop the item — rather than the model's opinion, which cannot be unit-tested
and is probed by hand with --dry-run instead.

The fixture is a real record, 4164 in the Seoul Metropolitan Archives, whose
post on 23 August 2026 is what prompted this check to be written.
"""

import unittest

import seoul_post


ITEM = {
    '_source': 'archives',
    'id': 4164,
    'title': '불량음료수 단속',
    'description': ('서울시경은 장마철에 나도는 전염병 방역의 일환으로 불량음료수 및 '
                    '빙과 판매행위, 부정식육 판매등을 일제 단속했다.'),
    'year': '1965',
}

SHIPPED = ('Seoul authorities targeted defective beverages, frozen goods and '
           'unlawful meat during monsoon season.')


class StrayYears(unittest.TestCase):
    """The one check that cannot itself hallucinate, so it must not misfire."""

    def test_a_year_the_record_states_is_not_stray(self):
        self.assertEqual(
            seoul_post.stray_years(ITEM, 'Crackdown of 1965', ''), [])

    def test_a_year_stated_only_in_the_korean_is_not_stray(self):
        # ⚠️ The \b trap. Korean writes 1965년 and Hangul is a word character,
        # so a \b-bounded pattern finds no boundary after the 5 and reads the
        # year in the source as absent — flagging every correctly dated
        # caption. Digit-bounded is why this passes.
        item = dict(ITEM, year='', description='1965년 여름에 단속했다.')
        self.assertEqual(seoul_post.stray_years(item, 'Crackdown of 1965'), [])

    def test_an_invented_year_is_caught(self):
        self.assertEqual(
            seoul_post.stray_years(ITEM, 'Crackdown of 1967', ''), ['1967'])

    def test_it_reads_the_description_as_well_as_the_title(self):
        self.assertEqual(
            seoul_post.stray_years(ITEM, '', 'Seized in 1972.'), ['1972'])

    def test_a_thousands_separated_quantity_is_not_a_year(self):
        # group_thousands writes 1,972 officials. Reading that as a year would
        # flag an accurate caption.
        self.assertEqual(
            seoul_post.stray_years(ITEM, '', '1,972 officials took part.'), [])

    def test_quantities_are_left_to_the_model(self):
        # 300만원 is three million won. A digit-for-digit test on quantities
        # calls the correct expansion invented, which is why only years are
        # checked here.
        item = dict(ITEM, description='상금 300만원을 준다.')
        self.assertEqual(
            seoul_post.stray_years(item, '', '3,000,000 won for the winner.'),
            [])

    def test_the_post_that_prompted_all_this_states_no_year(self):
        self.assertEqual(seoul_post.stray_years(ITEM, 'Defective Beverage '
                                                'Crackdown', SHIPPED), [])


class Verdicts(unittest.TestCase):
    """What translate_checked does with a problem, which is the user's
    decision of 23 August 2026: retry, then drop the description or the item."""

    def setUp(self):
        self.translations = []
        self.verdicts = []
        self.logged = []
        self._real = (seoul_post.translate, seoul_post.check_translation,
                      seoul_post.log_check)
        seoul_post.translate = lambda *a, **k: self.translations.pop(0)
        seoul_post.check_translation = lambda *a, **k: self.verdicts.pop(0)
        seoul_post.log_check = lambda *a: self.logged.append(a[-1])

    def tearDown(self):
        (seoul_post.translate, seoul_post.check_translation,
         seoul_post.log_check) = self._real

    def run_check(self, translations, verdicts):
        self.translations = list(translations)
        self.verdicts = list(verdicts)
        return seoul_post.translate_checked(ITEM, log=lambda *_: None)

    def test_a_clean_translation_goes_straight_through(self):
        out = self.run_check(
            [{'title': 'Defective drinks crackdown', 'description': SHIPPED,
              'date': ''}],
            [{'title': '', 'description': '', 'error': ''}])
        self.assertEqual(out[0], 'Defective drinks crackdown')
        self.assertEqual(self.logged, ['passed'])

    def test_a_flag_is_retried_once_and_the_retry_can_pass(self):
        out = self.run_check(
            [{'title': 'Wrong', 'description': 'Wrong.', 'date': ''},
             {'title': 'Right', 'description': 'Right.', 'date': ''}],
            [{'title': '', 'description': 'unsupported', 'error': ''},
             {'title': '', 'description': '', 'error': ''}])
        self.assertEqual(out[0], 'Right')
        self.assertEqual(self.logged, ['retranslated', 'passed'])

    def test_a_description_flagged_twice_is_dropped_and_the_title_posted(self):
        out = self.run_check(
            [{'title': 'Fine title', 'description': 'Bad.', 'date': ''},
             {'title': 'Fine title', 'description': 'Also bad.', 'date': ''}],
            [{'title': '', 'description': 'unsupported', 'error': ''},
             {'title': '', 'description': 'still unsupported', 'error': ''}])
        self.assertEqual(out, ('Fine title', '', ''))
        self.assertEqual(self.logged, ['retranslated', 'description dropped'])

    def test_a_title_flagged_twice_drops_the_item(self):
        out = self.run_check(
            [{'title': 'Bad', 'description': 'Fine.', 'date': ''},
             {'title': 'Still bad', 'description': 'Fine.', 'date': ''}],
            [{'title': 'misreads the Korean', 'description': '', 'error': ''},
             {'title': 'misreads the Korean', 'description': '', 'error': ''}])
        self.assertIsNone(out)
        self.assertEqual(self.logged, ['retranslated', 'redrawn'])

    def test_a_check_that_could_not_run_does_not_hold_up_the_post(self):
        # An unreachable checker is not a bad caption. The post goes out as
        # every post did before this existed, and the log says the check was
        # the thing that failed.
        out = self.run_check(
            [{'title': 'Title', 'description': 'Description.', 'date': ''}],
            [{'title': '', 'description': '', 'error': 'claude -p timed out'}])
        self.assertEqual(out[0], 'Title')
        self.assertEqual(self.logged, ['passed'])

    def test_the_date_survives_the_check(self):
        out = self.run_check(
            [{'title': 'Title', 'description': 'Description.',
              'date': '17 July 1968'}],
            [{'title': '', 'description': '', 'error': ''}])
        self.assertEqual(out[2], '17 July 1968')


if __name__ == '__main__':
    unittest.main()
