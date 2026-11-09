import unittest

from shop_client import charge_status, create_charge


class CheckoutTest(unittest.TestCase):
    def test_checkout_charges_customer(self):
        cid, status = create_charge(1999, "usd")
        self.assertTrue(cid)
        self.assertEqual(status, "succeeded")

    def test_order_page_shows_status(self):
        self.assertEqual(charge_status("ch_123"), "succeeded")


if __name__ == "__main__":
    unittest.main()
