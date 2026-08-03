import unittest

from src.news_pages import is_incomplete_article_text


class NewsPageCompletenessTest(unittest.TestCase):
    def test_feed_excerpt_markers_are_incomplete(self):
        text = (
            "In March 2021, I hosted a discussion […] "
            "The post New space wars appeared first on SpaceNews."
        )
        self.assertTrue(is_incomplete_article_text(text))

    def test_long_body_without_excerpt_markers_is_complete(self):
        self.assertFalse(is_incomplete_article_text("A complete article paragraph. " * 40))


if __name__ == "__main__":
    unittest.main()
