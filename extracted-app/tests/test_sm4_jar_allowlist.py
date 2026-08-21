import unittest

from recovery_service.services.ip_allowlist import ip_allowed


class IpAllowlistTests(unittest.TestCase):
    def test_empty_rules_only_allow_loopback(self) -> None:
        self.assertTrue(ip_allowed("127.0.0.1", ""))
        self.assertTrue(ip_allowed("::1", ""))
        self.assertFalse(ip_allowed("192.168.150.128", ""))

    def test_single_ip_cidr_range_and_list(self) -> None:
        rules = "192.168.150.128,10.20.0.0/16,172.16.1.20-172.16.1.30,2001:db8::/32"
        self.assertTrue(ip_allowed("192.168.150.128", rules))
        self.assertTrue(ip_allowed("10.20.3.9", rules))
        self.assertTrue(ip_allowed("172.16.1.25", rules))
        self.assertTrue(ip_allowed("2001:db8::42", rules))
        self.assertFalse(ip_allowed("172.16.1.31", rules))

    def test_invalid_rules_do_not_grant_access(self) -> None:
        self.assertFalse(ip_allowed("192.168.1.10", "bad-value,10.0.0.9-10.0.0.1"))

    def test_star_explicitly_allows_any_valid_ip(self) -> None:
        self.assertTrue(ip_allowed("203.0.113.8", "*"))
        self.assertFalse(ip_allowed("not-an-ip", "*"))


if __name__ == "__main__":
    unittest.main()
