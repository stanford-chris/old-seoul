"""Tests for item_year: where a record's year comes from.

Run from this directory:

    python3 -m unittest              # or: python3 test_item_year.py

Stdlib only, no network. The fixture is record 1691 in the Seoul Metropolitan
Archives, posted on 11 September 2026 as "date unknown" above a title reading
"1959년 국군 모범용사 환영회", which is what prompted the title fallback.
"""

import unittest

import seoul_post


ARCHIVES = seoul_post.SOURCES['archives']


class ItemYear(unittest.TestCase):

    def test_the_year_field_wins_when_present(self):
        self.assertEqual(seoul_post.item_year(
            {'title': '1959년 국군 모범용사 환영회', 'year': '1960'}), '1960')

    def test_a_title_leading_with_a_year_supplies_it(self):
        self.assertEqual(seoul_post.item_year(
            {'title': '1959년 국군 모범용사 환영회', 'year': None}), '1959')

    def test_a_title_leading_with_a_full_date_supplies_the_year(self):
        self.assertEqual(seoul_post.item_year(
            {'title': '1945년 9월 5일 공중에서 본 여의도공항', 'year': None}), '1945')

    def test_a_year_inside_the_title_is_not_taken(self):
        # The title names a thing, not a date; only a leading year is a date.
        self.assertEqual(seoul_post.item_year(
            {'title': '9.28 수복 10주년 (1950년 참전) 환영식', 'year': None}), '')

    def test_a_year_in_the_description_is_not_taken(self):
        self.assertEqual(seoul_post.item_year(
            {'title': '필리핀 빌리비드 수용소', 'year': None,
             'description': '일본군은 1941년 12월 진주만 기습으로'}), '')

    def test_a_bare_number_without_nyeon_is_not_a_year(self):
        self.assertEqual(seoul_post.item_year(
            {'title': '1959 국군 모범용사 환영회', 'year': None}), '')

    def test_no_year_anywhere_is_empty(self):
        self.assertEqual(seoul_post.item_year({'title': '불량음료수 단속'}), '')

    def test_the_header_reads_the_title_year(self):
        item = {'title': '1959년 국군 모범용사 환영회', 'year': None}
        self.assertEqual(seoul_post.post_header(item, ARCHIVES), '1959')

    def test_a_precise_day_still_beats_the_title_year(self):
        item = {'title': '1959년 국군 모범용사 환영회', 'year': None}
        self.assertEqual(seoul_post.post_header(item, ARCHIVES, '3 May 1959'),
                         '3 May 1959')


if __name__ == '__main__':
    unittest.main()
