
import unittest
from app.main import health


class HealthTest(unittest.TestCase):
    def test_health(self) -> None:
        self.assertEqual(health(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
