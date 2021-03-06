####################################################################################################
#                                                                                                  #
# (c) 2018, 2019 Quantstamp, Inc. This content and its use are governed by the license terms at    #
# <https://s3.amazonaws.com/qsp-protocol-license/V2_LICENSE.txt>                                   #
#                                                                                                  #
####################################################################################################

from helpers.qsp_test import QSPTest
from utils.eth.address import mk_checksum_address


class TestFile(QSPTest):

    def test_valid_address(self):
        """
        Tests that proper checksum is returned on valid address
        """
        address = "0xc1220b0bA0760817A9E8166C114D3eb2741F5949"
        result = mk_checksum_address(address)
        self.assertEqual(address, result)

    def test_error_address(self):
        """
        Tests that a value error is raised when the address is invalid
        """
        address = "this is nonsense"
        try:
            mk_checksum_address(address)
            self.fail("Expected value error on invalid address")
        except ValueError:
            # Expected
            pass

    def test_none(self):
        """
        Tests that None is returned when the argument is none
        """
        address = None
        result = mk_checksum_address(address)
        self.assertIsNone(result)
