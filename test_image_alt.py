"""Tests for the operator-aside strip in image_alt.

Run from this directory:

    python3 -m unittest              # or: python3 test_image_alt.py

Stdlib only, and no network or model call: `_strip_meta` is a pure string
function, so the cases below are the real ones written out verbatim.

Every case here is an incident or a near miss, not an invented edge case:

  - The model answers the operator instead of the reader. One such reply
    shipped to Bluesky on 16 August 2026 and a screen reader read the aside
    aloud before reaching the description.
  - The first guard could drop only two sentences from each end, and that
    aside was exactly two. A third sentence of preamble would have gone out
    with it, which is why the front of the strip is now unbounded.

The last three matter most. An over-eager strip is the worse bug of the two,
because it would quietly delete real description from live posts, and unlike
a leaked aside nobody would ever see what was lost.
"""

import unittest
import unittest.mock
import types
import subprocess

import image_alt

DESC = ('Black-and-white photograph of a wooden rooftop shed with wire-mesh '
        'cages, dozens of pigeons taking flight above the roofline.')

# Exactly what shipped on 16 August 2026, minus the tail the caller appends.
SHIPPED = ("Note: this image doesn't match the caption, it shows a rooftop "
           'pigeon coop, not a city plaza. Flagging that before giving alt '
           'text. ' + DESC)


class StripsOperatorAsides(unittest.TestCase):

    def strip(self, text):
        return image_alt._strip_meta(text, log=lambda *_: None).strip()

    def test_the_aside_that_actually_shipped(self):
        self.assertEqual(self.strip(SHIPPED), DESC)

    def test_curly_apostrophe(self):
        # A model emits "doesn’t" more often than "doesn't", and the pattern
        # for the contradiction remark only spells the straight form.
        self.assertEqual(self.strip(SHIPPED.replace("doesn't", 'doesn’t')), DESC)

    def test_contradiction_without_a_note_opener(self):
        # No "Note:" and no "alt text": the one case where the caption rule
        # has to catch it alone.
        self.assertEqual(
            self.strip('This image doesn’t match the caption. ' + DESC), DESC)

    def test_lead_in_ending_in_a_colon(self):
        # No sentence break after the colon, so sentence-level stripping
        # cannot see it. Handled by _LEAD_IN before the split.
        self.assertEqual(self.strip("Here's the alt text: " + DESC), DESC)

    def test_three_sentences_of_preamble(self):
        # The regression the two-sentence bound allowed: the third survived.
        self.assertEqual(self.strip(
            'Note: the caption is wrong. Flagging that before giving alt '
            'text. I should mention the mismatch. ' + DESC), DESC)

    def test_five_sentences_of_preamble(self):
        self.assertEqual(self.strip(
            'Sure, I can help with that. Note: the caption is wrong. '
            'Flagging that before giving alt text. I should mention the '
            'mismatch. Here is my alt text attempt. ' + DESC), DESC)

    def test_trailing_sign_off(self):
        self.assertEqual(
            self.strip(DESC + ' Let me know if you want it shorter.'), DESC)


class LeavesRealDescriptionsAlone(unittest.TestCase):
    """The failure that would never be noticed, so it is tested hardest."""

    def strip(self, text):
        return image_alt._strip_meta(text, log=lambda *_: None).strip()

    def test_one_sentence_is_untouched(self):
        self.assertEqual(self.strip(DESC), DESC)

    def test_two_sentences_are_untouched(self):
        text = ('Black-and-white photograph of a crowded street corner. Two '
                'figures stand beneath a striped awning at the right.')
        self.assertEqual(self.strip(text), text)

    def test_a_reply_that_is_meta_throughout_returns_nothing(self):
        # Not a salvaged fragment: too short for describe() to accept, so the
        # caller falls back to the citation, which is the right outcome.
        out = self.strip('Note: the caption is wrong. Flagging that before '
                         'giving alt text. Let me know if you want another.')
        self.assertLess(len(out), image_alt.MIN_CHARS)


class FakeRun:
    """Stands in for subprocess.run, answering by what the prompt asks for.

    Keyed on the prompt rather than on call order, so the stub cannot decide
    what the code under test sees: a change in the number of calls shows up as
    a wrong answer rather than being silently absorbed.
    """

    def __init__(self, describe_replies, verify_replies):
        self.describe_replies = list(describe_replies)
        self.verify_replies = list(verify_replies)
        self.prompts = []

    def __call__(self, argv, **kw):
        prompt = argv[-1]
        self.prompts.append(prompt)
        pool = (self.verify_replies if prompt.startswith('Look at the image')
                else self.describe_replies)
        reply = pool.pop(0)
        if isinstance(reply, Exception):
            raise reply
        rc, out = reply
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr='')

    @property
    def verify_prompts(self):
        return [p for p in self.prompts if p.startswith('Look at the image')]

    @property
    def describe_prompts(self):
        return [p for p in self.prompts if not p.startswith('Look at the image')]


GOOD = ('Black-and-white photograph of a snow-covered wooden hall '
        'behind a bare tree.')
WORSE = ('Black-and-white photograph of a snow-covered wooden hall behind '
         'a bare tree, with a small figure walking in front.')
ALL_FOUND = 'FOUND | wooden hall | center\nFOUND | bare tree | foreground'
ONE_ABSENT = ('FOUND | wooden hall | center\n'
              'ABSENT | a small figure walking | it is the trunk of the tree')


class Unsupported(unittest.TestCase):
    """The verifier's three answers: clean, faulty, and could-not-tell.

    The third is the one that matters. A failed call yields no ABSENT lines,
    which is byte-identical to a clean check unless something insists on the
    difference — the same shape as portfolio_brief.py publishing "$0.00" over
    a good note because a denied read came back as an empty list.
    """

    def call(self, reply):
        fake = FakeRun([], [reply])
        with unittest.mock.patch.object(image_alt.subprocess, 'run', fake):
            return image_alt._unsupported(
                b'jpegbytes', GOOD, env=None, model='m', timeout=1,
                suffix='.jpg', log=lambda *_: None)

    def test_all_found_is_clean(self):
        self.assertEqual(self.call((0, ALL_FOUND)), [])

    def test_absent_lines_are_returned(self):
        self.assertEqual(self.call((0, ONE_ABSENT)), ['a small figure walking'])

    def test_a_reply_with_no_verdicts_is_not_a_pass(self):
        # The whole point. "Sure, that looks accurate to me!" carries no
        # ABSENT lines and must not read as a clean verification.
        self.assertIsNone(self.call((0, 'Sure, that looks accurate to me!')))

    def test_empty_reply_is_not_a_pass(self):
        self.assertIsNone(self.call((0, '')))

    def test_nonzero_exit_is_not_a_pass(self):
        self.assertIsNone(self.call((1, 'error: overloaded')))

    def test_timeout_is_not_a_pass(self):
        self.assertIsNone(self.call(
            subprocess.TimeoutExpired(cmd='claude', timeout=1)))


class DescribeVerification(unittest.TestCase):
    """describe() around the check: ship, retry, drop, or ship unverified."""

    def run_describe(self, describe_replies, verify_replies, **kw):
        fake = FakeRun(describe_replies, verify_replies)
        with unittest.mock.patch.object(image_alt.subprocess, 'run', fake):
            out = image_alt.describe(b'jpegbytes', context='a caption',
                                     log=lambda *_: None, **kw)
        return out, fake

    def test_a_verified_description_ships_unchanged(self):
        out, fake = self.run_describe([(0, GOOD)], [(0, ALL_FOUND)])
        self.assertEqual(out, GOOD)
        self.assertEqual(len(fake.verify_prompts), 1)

    def test_a_failed_check_regenerates_once_and_ships_the_retry(self):
        out, fake = self.run_describe(
            [(0, WORSE), (0, GOOD)], [(0, ONE_ABSENT), (0, ALL_FOUND)])
        self.assertEqual(out, GOOD)
        self.assertEqual(len(fake.describe_prompts), 2)

    def test_the_retry_names_what_failed(self):
        _, fake = self.run_describe(
            [(0, WORSE), (0, GOOD)], [(0, ONE_ABSENT), (0, ALL_FOUND)])
        self.assertIn('a small figure walking', fake.describe_prompts[1])

    def test_two_failures_drop_the_description(self):
        # The caller falls back to its citation. A plain attribution beats a
        # confident sentence about someone who is not in the photograph.
        out, fake = self.run_describe(
            [(0, WORSE), (0, WORSE)], [(0, ONE_ABSENT), (0, ONE_ABSENT)])
        self.assertIsNone(out)
        self.assertEqual(len(fake.describe_prompts), 2)

    def test_an_unmakeable_check_ships_the_description(self):
        # Never the thing that ends a run: a verifier having a bad morning
        # must not strip descriptions off every post.
        out, _ = self.run_describe([(0, GOOD)], [(1, 'overloaded')])
        self.assertEqual(out, GOOD)

    def test_an_unmakeable_check_says_so(self):
        lines = []
        fake = FakeRun([(0, GOOD)], [(1, 'overloaded')])
        with unittest.mock.patch.object(image_alt.subprocess, 'run', fake):
            image_alt.describe(b'jpegbytes', log=lines.append)
        self.assertTrue(any('UNVERIFIED' in ln for ln in lines), lines)

    def test_verify_false_makes_no_check_at_all(self):
        out, fake = self.run_describe([(0, GOOD)], [], verify=False)
        self.assertEqual(out, GOOD)
        self.assertEqual(fake.verify_prompts, [])

    def test_the_verifier_is_never_given_the_caption(self):
        # The caption is where imported facts come from. A verifier holding it
        # will confirm them, which is the failure this check exists to catch.
        _, fake = self.run_describe([(0, GOOD)], [(0, ALL_FOUND)])
        self.assertNotIn('a caption', fake.verify_prompts[0])

    def test_the_verifier_is_given_the_description(self):
        _, fake = self.run_describe([(0, GOOD)], [(0, ALL_FOUND)])
        self.assertIn(GOOD, fake.verify_prompts[0])

    def test_a_failed_generation_never_reaches_the_check(self):
        out, fake = self.run_describe([(1, 'boom')], [])
        self.assertIsNone(out)
        self.assertEqual(fake.verify_prompts, [])

    def test_a_timeout_retries_once_and_ships_the_retry(self):
        # A verification failure already got one retry; a raw call failure
        # did not, so a single transient timeout dropped the description
        # outright. This is the fix: one retry, same as the check gets.
        out, fake = self.run_describe(
            [subprocess.TimeoutExpired(cmd='claude', timeout=1), (0, GOOD)],
            [(0, ALL_FOUND)])
        self.assertEqual(out, GOOD)
        self.assertEqual(len(fake.describe_prompts), 2)

    def test_two_timeouts_give_up(self):
        out, fake = self.run_describe(
            [subprocess.TimeoutExpired(cmd='claude', timeout=1),
             subprocess.TimeoutExpired(cmd='claude', timeout=1)], [])
        self.assertIsNone(out)
        self.assertEqual(fake.verify_prompts, [])

if __name__ == '__main__':
    unittest.main()
