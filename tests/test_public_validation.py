import unittest

from scripts.validate_public_fixtures import main as validate_public_fixtures


class PublicValidationTests(unittest.TestCase):
    def test_public_fixture_validation_script(self) -> None:
        self.assertEqual(validate_public_fixtures(), 0)


if __name__ == "__main__":
    unittest.main()
