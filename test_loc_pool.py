"""Tests for the Library of Congress pool: the English-source caption path,
the placeholder-date guard, the stereograph frame crop and the pool wiring.

Run from this directory:

    python3 -m unittest              # or: python3 test_loc_pool.py

Stdlib plus Pillow (for the crop). No network and no model call:
translate_to_korean and check_korean are stubbed, so what is tested is what
korean_line_checked decides, not the model's opinion. The fixture is a real
record, the Underwood & Underwood stereograph "Horseshoeing on the 'safety'
plan - a blacksmith shop outside the East Gate, Seoul, Korea" of 1904, in the
shape seoul_loc_harvest.py writes.
"""

import io
import unittest

import seoul_post

LOC = seoul_post.SOURCES['loc']

ITEM = {
    '_source': 'loc',
    'id': '2019633106',
    'title': "Horseshoeing on the 'safety' plan - a blacksmith shop outside "
             "the East Gate, Seoul, Korea",
    'year': '1904',
    'date': '1904-01-01',
    'created_published': 'New York : Underwood & Underwood, c1904.',
    'description': '',
    'collection': ['stereograph cards'],
    'stereo': True,
    'seoul_named': True,
    'image_url': 'https://tile.loc.gov/x/1s09846r.jpg',
    'detail_url': 'https://www.loc.gov/item/2019633106/',
    'rights': 'No known restrictions on publication.',
}


class PoolWiring(unittest.TestCase):

    def test_only_items_naming_seoul_are_selected(self):
        self.assertTrue(LOC['select'](dict(ITEM)))
        self.assertFalse(LOC['select'](dict(ITEM, seoul_named=False)))
        self.assertFalse(LOC['select']({'title': 'no flag at all'}))

    def test_the_pool_holds_a_configured_share(self):
        # 2 LoC items against 98 archive items: an unweighted draw would give
        # the pool 2%, the share gives it 10%.
        cands = [dict(ITEM)] * 2 + [{'_source': 'archives'}] * 98
        weights = seoul_post.draw_weights(cands)
        self.assertAlmostEqual(sum(weights[:2]), 0.10, places=6)

    def test_the_link_rides_on_the_record_s_own_url(self):
        self.assertEqual(seoul_post.item_link(ITEM, LOC),
                         'https://www.loc.gov/item/2019633106/')


class PlaceholderDate(unittest.TestCase):

    def test_a_loc_first_of_january_is_not_a_day(self):
        # The catalogue pads '1904' to '1904-01-01'. Read as a day it would
        # head the post "1 January 1904".
        self.assertEqual(seoul_post.item_date_en(ITEM), '')

    def test_a_gazette_day_is_still_a_day(self):
        self.assertEqual(seoul_post.item_date_en(
            {'_source': 'gazette', 'date': '1982-01-07'}), '7 January 1982')

    def test_the_header_is_the_year(self):
        self.assertEqual(seoul_post.post_header(ITEM, LOC), '1904')

    def test_a_year_range_from_the_record_is_printed_as_one(self):
        self.assertEqual(seoul_post.post_header(
            dict(ITEM, year='1910-1920'), LOC), '1910-1920')


class KoreanLine(unittest.TestCase):
    """What korean_line_checked does with a verdict: the mirror of the
    Verdicts class in test_translation_check.py."""

    def setUp(self):
        self.lines = []
        self.verdicts = []
        self.logged = []
        self._real = (seoul_post.translate_to_korean, seoul_post.check_korean,
                      seoul_post.log_check)
        seoul_post.translate_to_korean = lambda *a, **k: self.lines.pop(0)
        seoul_post.check_korean = lambda *a, **k: self.verdicts.pop(0)
        seoul_post.log_check = lambda *a: self.logged.append(a[-1])

    def tearDown(self):
        (seoul_post.translate_to_korean, seoul_post.check_korean,
         seoul_post.log_check) = self._real

    def run_check(self, lines, verdicts, item=None):
        self.lines = list(lines)
        self.verdicts = list(verdicts)
        self.item = dict(item or ITEM)
        return seoul_post.translate_checked(self.item, log=lambda *_: None)

    def test_the_english_is_the_source_and_ships_verbatim_after_the_style_pass(self):
        out = self.run_check([{'title': '동대문 밖 대장간'}],
                             [{'title': '', 'description': '', 'error': ''}])
        self.assertEqual(out, (
            'Horseshoeing on the “safety” plan - a blacksmith shop outside '
            'the East Gate, Seoul, Korea', '', ''))
        self.assertEqual(self.item['_line_ko'], '동대문 밖 대장간')
        self.assertEqual(seoul_post.source_line(self.item), '동대문 밖 대장간')
        self.assertEqual(self.logged, ['passed'])

    def test_a_flag_is_retried_once_and_the_retry_can_pass(self):
        out = self.run_check(
            [{'title': '틀린 줄'}, {'title': '맞는 줄'}],
            [{'title': 'names the wrong gate', 'description': '', 'error': ''},
             {'title': '', 'description': '', 'error': ''}])
        self.assertIsNotNone(out)
        self.assertEqual(self.item['_line_ko'], '맞는 줄')
        self.assertEqual(self.logged, ['retranslated', 'passed'])

    def test_a_line_flagged_twice_redraws_the_item(self):
        # No "post the English alone" branch: every post here is bilingual.
        out = self.run_check(
            [{'title': '틀린 줄'}, {'title': '또 틀린 줄'}],
            [{'title': 'wrong', 'description': '', 'error': ''},
             {'title': 'still wrong', 'description': '', 'error': ''}])
        self.assertIsNone(out)
        self.assertNotIn('_line_ko', self.item)
        self.assertEqual(self.logged, ['retranslated', 'redrawn'])

    def test_an_empty_korean_counts_as_a_failure(self):
        out = self.run_check(
            [{'title': ''}, {'title': ''}],
            [{'title': '', 'description': '', 'error': ''},
             {'title': '', 'description': '', 'error': ''}])
        self.assertIsNone(out)

    def test_a_check_that_could_not_run_does_not_hold_up_the_post(self):
        out = self.run_check([{'title': '동대문 밖 대장간'}],
                             [{'title': '', 'description': '',
                               'error': 'claude -p timed out'}])
        self.assertIsNotNone(out)
        self.assertEqual(self.logged, ['passed'])

    def test_a_description_the_record_carries_ships_in_english(self):
        item = dict(ITEM, description='Photograph shows a smith at work with '
                                      'the horse slung in a frame.')
        out = self.run_check([{'title': '동대문 밖 대장간'}],
                             [{'title': '', 'description': '', 'error': ''}],
                             item)
        self.assertTrue(out[1].startswith('Photograph shows a smith'))

    def test_the_source_line_falls_back_to_the_title_for_a_korean_pool(self):
        self.assertEqual(seoul_post.source_line(
            {'_source': 'archives', 'title': '불량음료수 단속'}), '불량음료수 단속')


class KoreanCheck(unittest.TestCase):

    def test_a_year_the_korean_invents_is_caught_without_the_model(self):
        # Deterministic, so no stub needed and the model gets no vote.
        out = seoul_post.check_korean(ITEM, '1907년 동대문 밖 대장간',
                                      log=lambda *_: None)
        self.assertIn('1907', out['title'])

    def test_the_record_s_own_year_is_not_stray(self):
        self.assertEqual(seoul_post.stray_years(ITEM, '1904년 동대문 밖 대장간'), [])


class PostShape(unittest.TestCase):

    def test_the_post_carries_the_english_the_korean_and_the_credit(self):
        item = dict(ITEM, _line_ko='동대문 밖 대장간')
        tb = seoul_post.format_post(
            seoul_post.house_style(item['title']), '', seoul_post.source_line(item),
            seoul_post.post_header(item, LOC), item, LOC)
        text = tb.build_text()
        self.assertTrue(text.startswith('1904\n\n'))
        self.assertIn('blacksmith shop outside the East Gate', text)
        self.assertIn('\n동대문 밖 대장간\n', text)
        self.assertIn('#photography', text)
        self.assertTrue(text.endswith('🗃️ Library of Congress'))
        self.assertLessEqual(len(text), 300)

    def test_the_alt_lead_names_the_stereograph_frame(self):
        self.assertEqual(
            seoul_post.alt_tail(LOC, ITEM, '1904'),
            'Library of Congress, Prints and Photographs Division, '
            'stereograph, one frame of the pair, 1904.')
        self.assertEqual(
            seoul_post.alt_tail(LOC, dict(ITEM, stereo=False), '1895'),
            'Library of Congress, Prints and Photographs Division, 1895.')


def _card(width=1000, height=500, mount=(235, 225, 200)):
    """A synthetic stereograph: a flat mount with two textured frames and a
    caption strip, in the proportions of the real cards."""
    from PIL import Image, ImageDraw
    import random
    rnd = random.Random(4)
    im = Image.new('RGB', (width, height), mount)
    px = im.load()
    frames = [(60, 40, 470, 380), (530, 40, 940, 380)]
    for (x0, y0, x1, y1) in frames:
        for x in range(x0, x1):
            for y in range(y0, y1):
                v = rnd.randint(20, 200)
                px[x, y] = (v, v, v)
    # A caption strip under the frames, in the mount's own ink.
    ImageDraw.Draw(im).text((70, 420), 'SEOUL, KOREA.', fill=(60, 60, 60))
    out = io.BytesIO()
    im.save(out, format='JPEG', quality=92)
    return out.getvalue(), frames[0]


class StereoFrame(unittest.TestCase):

    def test_the_left_frame_is_cut_from_the_card(self):
        from PIL import Image
        data, (x0, y0, x1, y1) = _card()
        out = Image.open(io.BytesIO(seoul_post.stereo_frame(data, log=lambda *_: None)))
        # Within the pad of the frame's true edges, and nowhere near the
        # right-hand frame or the caption strip.
        self.assertLessEqual(abs(out.width - (x1 - x0)), 2 * seoul_post.STEREO_PAD + 2)
        self.assertLessEqual(abs(out.height - (y1 - y0)), 2 * seoul_post.STEREO_PAD + 2)

    def test_a_dark_mount_is_found_the_same_way(self):
        from PIL import Image
        data, (x0, y0, x1, y1) = _card(mount=(40, 40, 45))
        out = Image.open(io.BytesIO(seoul_post.stereo_frame(data, log=lambda *_: None)))
        self.assertLessEqual(abs(out.width - (x1 - x0)), 2 * seoul_post.STEREO_PAD + 2)

    def test_a_frame_the_detector_cannot_see_falls_back_to_the_left_half(self):
        from PIL import Image
        im = Image.new('RGB', (800, 400), (200, 200, 200))
        buf = io.BytesIO(); im.save(buf, format='JPEG')
        out = Image.open(io.BytesIO(seoul_post.stereo_frame(buf.getvalue(), log=lambda *_: None)))
        self.assertEqual(out.size, (400, 400))

    def test_only_stereographs_are_cropped(self):
        # The flag is per item, not per pool: the 1895 prints are whole
        # photographs and must arrive untouched.
        self.assertTrue(LOC.get('crop_stereo'))
        self.assertFalse(dict(ITEM, stereo=False)['stereo'])


class PoolWriteBack(unittest.TestCase):

    def test_every_underscore_key_is_an_in_memory_tag(self):
        item = dict(ITEM, _line_ko='동대문 밖 대장간')
        kept = {k: v for k, v in item.items() if not k.startswith('_')}
        self.assertNotIn('_source', kept)
        self.assertNotIn('_line_ko', kept)
        self.assertIn('rights', kept)


if __name__ == '__main__':
    unittest.main()
