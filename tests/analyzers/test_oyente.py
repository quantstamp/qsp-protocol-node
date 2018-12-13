####################################################################################################
#                                                                                                  #
# (c) 2018 Quantstamp, Inc. All rights reserved.  This content shall not be used, copied,          #
# modified, redistributed, or otherwise disseminated except to the extent expressly authorized by  #
# Quantstamp for credentialed users. This content and its use are governed by the Quantstamp       #
# Demonstration License Terms at <https://s3.amazonaws.com/qsp-protocol-license/LICENSE.txt>.      #
#                                                                                                  #
####################################################################################################

"""
Tests invocation of the analyzer tool.
"""
import unittest
import json

from random import random
from time import time
from helpers.resource import project_root
from helpers.resource import resource_uri
from helpers.qsp_test import QSPTest
from audit import Analyzer, Wrapper
from utils.io import fetch_file


class TestAnalyzerOyente(QSPTest):
    """
    Asserts different properties over Analyzer objects.
    """

    @staticmethod
    def __new_analyzer(storage_dir="/tmp", timeout_sec=120):
        oyente_wrapper = Wrapper(
            wrappers_dir="{0}/analyzers/wrappers".format(project_root()),
            analyzer_name="oyente",
            args="-ce",
            storage_dir="/tmp/{}{}".format(time(), random()),
            timeout_sec=timeout_sec,
        )
        return Analyzer(oyente_wrapper)

    def test_report_creation(self):
        """
        Tests whether a report is created upon calling the analyzer
        on a buggy contract
        """
        analyzer = TestAnalyzerOyente.__new_analyzer()

        buggy_contract = fetch_file(resource_uri("DAOBug.sol"))
        request_id = 15
        report = analyzer.check(buggy_contract, request_id, "DAOBug.sol")

        # Asserts some result produced
        self.assertTrue(report)

        print(json.dumps(report, indent=2))

        # Asserts result is success
        self.assertTrue(report['status'], 'success')
        self.assertIsNotNone(report['potential_vulnerabilities'])
        self.assertEquals(1, len(report['potential_vulnerabilities']))

    def test_file_not_found(self):
        """
        Tests whether an exception is raised upon calling the analyzer
        on a non-existent file
        """

        no_file = str(random()) + ".sol"
        analyzer = TestAnalyzerOyente.__new_analyzer()
        request_id = 15
        report = analyzer.check(no_file, request_id, no_file)

        self.assertTrue(report['status'], 'error')
        self.assertTrue("No such file or directory" in ''.join(err + ' ' for err in report['errors']))

    def test_old_pragma(self):
        """
        Tests whether an exception is raised upon calling the analyzer
        with a contract locking an old version of Solidity.
        """

        old_contract = fetch_file(resource_uri("DAOBugOld.sol"))
        analyzer = TestAnalyzerOyente.__new_analyzer()
        request_id = 15
        report = analyzer.check(old_contract, request_id, "DAOBugOld.sol")

        self.assertTrue(report['status'], 'error')
        self.assertTrue(1, len(report['errors']))
        self.assertTrue("Source file requires different compiler version" in report['errors'][0])

    def test_old_pragma_with_caret(self):
        """
        Tests whether no exception is raised upon calling the analyzer
        with a contract locking an old version of Solidity with caret.
        """

        old_contract = fetch_file(resource_uri("DAOBugOld-Caret.sol"))
        analyzer = TestAnalyzerOyente.__new_analyzer()
        request_id = 15
        report = analyzer.check(old_contract, request_id, "DAOBugOld-Caret.sol")

        self.assertTrue(report['status'], 'success')
        self.assertEquals(1, len(report['potential_vulnerabilities']))

    def test_get_metadata(self):
        analyzer = TestAnalyzerOyente.__new_analyzer()
        metadata = analyzer.get_metadata("x", 1, "x")
        self.assertTrue("name" in metadata.keys())
        self.assertTrue("version" in metadata.keys())
        self.assertTrue("vulnerabilities_checked" in metadata.keys())
        self.assertTrue("command" in metadata.keys())


if __name__ == '__main__':
    unittest.main()
