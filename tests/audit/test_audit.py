"""
Tests the flow of receiving audit requests and
their flow within the QSP audit node
"""
import contextlib
import os
import subprocess
import unittest
import ntpath

from timeout_decorator import timeout
from threading import Thread
from time import sleep

from audit import QSPAuditNode
from audit import ExecutionException
from config import ConfigFactory, ConfigUtils, ConfigurationException
from dpath.util import get
from helpers.resource import (
    resource_uri,
    project_root,
)
from pathlib import Path
from solc import compile_files
from utils.io import fetch_file, digest_file, load_json
from utils.db import get_first
from utils.db import Sqlite3Worker
from deepdiff import DeepDiff
from pprint import pprint
from utils.eth import DeduplicationException


class TestQSPAuditNode(unittest.TestCase):
    __AUDIT_STATE_SUCCESS = 4
    __AUDIT_STATE_ERROR = 5
    __AVAILABLE_AUDIT__STATE_READY = 1
    __AVAILABLE_AUDIT__STATE_ERROR = 0
    __REQUEST_ID = 1
    __PRICE = 100

    @classmethod
    def __clean_up_pool_db(cls, evt_db_path):
        print("Cleaning test database")
        with contextlib.suppress(FileNotFoundError):
            os.remove(evt_db_path)

    @staticmethod
    def __load_audit_contract_from_src(web3_client, contract_src_uri, contract_name, data_contract_name,
                                       constructor_from):
        """
        Loads the QuantstampAuditMock contract from source code returning the (address, contract)
        pair.
        """
        audit_contract_src = fetch_file(contract_src_uri)
        contract_dict = compile_files([audit_contract_src])
        contract_id = "{0}:{1}".format(
            contract_src_uri,
            contract_name,
        )
        data_contract_id = "{0}:{1}".format(
            contract_src_uri,
            data_contract_name,
        )
        contract_interface = contract_dict[contract_id]
        data_contract_interface = contract_dict[data_contract_id]

        # deploy the data contract
        data_contract = web3_client.eth.contract(
            abi=data_contract_interface['abi'],
            bytecode=data_contract_interface['bin']
        )
        tx_hash = data_contract.constructor().transact({'from': constructor_from, 'gasPrice': 0})
        receipt = web3_client.eth.getTransactionReceipt(tx_hash)
        data_address = receipt['contractAddress']
        data_contract = web3_client.eth.contract(
            abi=data_contract_interface['abi'],
            address=data_address,
        )

        # deploy the audit contract
        audit_contract = web3_client.eth.contract(
            abi=contract_interface['abi'],
            bytecode=contract_interface['bin']
        )
        tx_hash = audit_contract.constructor(data_address).transact({'from': constructor_from, 'gasPrice': 0})
        receipt = web3_client.eth.getTransactionReceipt(tx_hash)
        address = receipt['contractAddress']
        audit_contract = web3_client.eth.contract(
            abi=contract_interface['abi'],
            address=address,
        )
        return address, audit_contract, data_contract

    @classmethod
    def fetch_config(cls):
        # create config from file, the contract is not provided and will be injected separately
        config_file_uri = resource_uri("test_config.yaml")
        config = ConfigFactory.create_from_file("local", config_file_uri,
                                                validate_contract_settings=False)
        # compile and inject contract
        contract_source_uri = "./tests/resources/QuantstampAuditMock.sol"
        contract_metadata_uri = "./tests/resources/QuantstampAudit-metadata.json"
        data_contract_metadata_uri = "./tests/resources/QuantstampAuditData-metadata.json"
        audit_contract_metadata = load_json(fetch_file(contract_metadata_uri))
        data_contract_metadata = load_json(fetch_file(data_contract_metadata_uri))
        audit_contract_name = get(audit_contract_metadata, '/contractName')
        data_contract_name = get(data_contract_metadata, '/contractName')
        config._Config__audit_contract_address, config._Config__audit_contract, config._Config__audit_data_contract = \
            TestQSPAuditNode.__load_audit_contract_from_src(
                config.web3_client,
                contract_source_uri,
                audit_contract_name,
                data_contract_name,
                config.account,)
        config_utils = ConfigUtils(config.node_version)
        config_utils.check_configuration_settings(config)
        return config

    @classmethod
    def setUpClass(cls):
        config = TestQSPAuditNode.fetch_config()
        TestQSPAuditNode.__clean_up_pool_db(config.evt_db_path)

        all_wrappers_dir = Path("{0}/analyzers/wrappers".format(project_root()))

        i = 0
        # Iterate over all the wrappers within the analyzers folder
        for entry in all_wrappers_dir.iterdir():
            if not entry.is_dir():
                continue

            wrapper_home = entry

            env = dict(os.environ)
            env['WRAPPER_HOME'] = wrapper_home

            storage_dir = config.analyzers[i].wrapper.storage_dir
            env['STORAGE_DIR'] = storage_dir

            # Creates storage directory if it does not exits
            if not os.path.exists(storage_dir):
                os.makedirs(storage_dir)

            once_process = subprocess.run(
                "{0}/once".format(wrapper_home),
                shell=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )

            if once_process.returncode == 1:
                # An error occurred
                raise Exception(
                    "Error invoking once (return value is not 0). Output is {0}".format(
                        once_process.stdout))

            i += 1

    def setUp(self):
        """
        Starts the execution of the QSP audit node as a separate thread.
        """
        self.__config = TestQSPAuditNode.fetch_config()
        self.__audit_node = QSPAuditNode(self.__config)
        self.maxDiff = None

        self.__getNextAuditRequest_filter = self.__config.audit_contract.events.getNextAuditRequest_called.createFilter(
            fromBlock=max(0, self.__config.event_pool_manager.get_latest_block_number())
        )
        self.__submitReport_filter = self.__config.audit_contract.events.submitReport_called.createFilter(
            fromBlock=max(0, self.__config.event_pool_manager.get_latest_block_number())
        )
        self.__setAnyRequestAvailableResult_filter = self.__config.audit_contract.events.setAnyRequestAvailableResult_called.createFilter(
            fromBlock=max(0, self.__config.event_pool_manager.get_latest_block_number())
        )
        self.__setAuditNodePrice_filter = self.__config.audit_contract.events.setAuditNodePrice_called.createFilter(
            fromBlock=max(0, self.__config.event_pool_manager.get_latest_block_number())
        )

        def exec():
            self.__audit_node.run()

        audit_node_thread = Thread(target=exec, name="Audit node")
        audit_node_thread.start()

        max_initialization_seconds = 5
        num_checks = 0
        while not self.__audit_node.audit_node_initialized:
            sleep(1)
            num_checks += 1
            if num_checks == max_initialization_seconds:
                raise Exception("Node threads could not be initialized")

    def tearDown(self):
        """
        Stops the execution of the current QSP audit node.
        """
        self.__audit_node.stop()
        TestQSPAuditNode.__clean_up_pool_db(self.__config.evt_db_path)

    # @timeout(5, timeout_exception=StopIteration)
    # def test_timeout_stale_events(self):
    #     class Eth:
    #         def __init__(self):
    #             self.blockNumber = 100 # <<<---- THIS IS NOT SAFE! Let's seek alternatives

    #     # Stops the audit node
    #     self.__audit_node.stop()

    #     # Resets some configuration
    #     self.__audit_node._QSPAuditNode__config.web3_client.eth = Eth()
    #     self.__audit_node._QSPAuditNode__config._Config__submission_timeout_limit_blocks = 10
    #     self.__audit_node._QSPAuditNode__config._Config__block_discard_on_restart = 2
        
    #     # Certainly times out
    #     evt_first = {'request_id': 1,
    #                  'requestor': 'x',
    #                  'contract_uri': 'x',
    #                  'evt_name': 'x',
    #                  'block_nbr': 10,
    #                  'status_info': 'x',
    #                  'price': 12}
    #     # Last block to time out
    #     evt_second = {'request_id': 17,
    #                   'requestor': 'x',
    #                   'contract_uri': 'x',
    #                   'evt_name': 'x',
    #                   'block_nbr': 90 + 2,
    #                   'status_info': 'x',
    #                   'price': 12}
    #     # First block not to time out
    #     evt_third = {'request_id': 18,
    #                  'requestor': 'x',
    #                  'contract_uri': 'x',
    #                  'evt_name': 'x',
    #                  'block_nbr': 91 + 2,
    #                  'status_info': 'x',
    #                  'price': 12}

    #     # Restarts the audit node
    #     self.__audit_node.run()

    #     self.__audit_node._QSPAuditNode__config.event_pool_manager.add_evt_to_be_assigned(evt_first)
    #     self.__audit_node._QSPAuditNode__config.event_pool_manager.add_evt_to_be_assigned(
    #         evt_second
    #     )
    #     self.__audit_node._QSPAuditNode__config.event_pool_manager.add_evt_to_be_assigned(evt_third)
    #     self.__audit_node._QSPAuditNode__config.event_pool_manager.sql3lite_worker.execute(
    #         "update audit_evt set fk_status = 'TS' where request_id = 1"
    #     )
    #     self.__audit_node._QSPAuditNode__timeout_stale_requests()
    #     fst = self.__audit_node._QSPAuditNode__config.event_pool_manager.get_event_by_request_id(
    #         evt_first['request_id'])
    #     snd = self.__audit_node._QSPAuditNode__config.event_pool_manager.get_event_by_request_id(
    #         evt_second['request_id'])
    #     thrd = self.__audit_node._QSPAuditNode__config.event_pool_manager.get_event_by_request_id(
    #         evt_third['request_id'])
        
    #     self.assertEqual(fst['fk_status'], 'ER')
    #     self.assertEqual(snd['fk_status'], 'ER')
    #     self.assertEqual(thrd['fk_status'], 'AS')

    @timeout(10, timeout_exception=StopIteration)
    def test_check_then_request_audit_request_exceptions(self):
        # The following causes an exception in the auditing node, but it should be caught and
        # should not propagate
        get_next_audit_request = self.__audit_node._QSPAuditNode__get_next_audit_request

        def mocked__get_next_audit_request():
            raise Exception('mocked exception')

        self.__audit_node._QSPAuditNode__get_next_audit_request = mocked__get_next_audit_request
        
        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__config.audit_contract.functions.setAnyRequestAvailableResult(
            self.__AVAILABLE_AUDIT__STATE_READY).transact({"from": self.__config.account})
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        self.__evt_wait_loop(self.__setAnyRequestAvailableResult_filter)

        self.__audit_node._QSPAuditNode__web3_lock()
        self.__audit_node._QSPAuditNode__check_then_request_audit_request()
        tx_hash = self.__config.audit_contract.functions.setAnyRequestAvailableResult(
                self.__AVAILABLE_AUDIT__STATE_ERROR).transact({"from": self.__config.account})
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        self.__evt_wait_loop(self.__setAnyRequestAvailableResult_filter)
        self.__audit_node._QSPAuditNode__get_next_audit_request = get_next_audit_request

    @timeout(10, timeout_exception=StopIteration)
    def test__check_then_request_audit_request_deduplication_exceptions(self):
        # The following causes an exception in the auditing node, but it should be caught and
        # should not propagate
        get_next_audit_request = self.__audit_node._QSPAuditNode__get_next_audit_request

        def mocked__get_next_audit_request():
            raise DeduplicationException('mocked exception')

        self.__audit_node._QSPAuditNode__get_next_audit_request = mocked__get_next_audit_request

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__config.audit_contract.functions.setAnyRequestAvailableResult(
            self.__AVAILABLE_AUDIT__STATE_READY).transact({"from": self.__config.account})
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)        
        self.__evt_wait_loop(self.__setAnyRequestAvailableResult_filter)

        self.__audit_node._QSPAuditNode__web3_lock()
        self.__audit_node._QSPAuditNode__check_then_request_audit_request()
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__config.audit_contract.functions.setAnyRequestAvailableResult(
                self.__AVAILABLE_AUDIT__STATE_ERROR).transact({"from": self.__config.account})
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        self.__evt_wait_loop(self.__setAnyRequestAvailableResult_filter)
        self.__audit_node._QSPAuditNode__get_next_audit_request = get_next_audit_request

    @timeout(20, timeout_exception=StopIteration)
    def test_on_audit_assigned(self):
        # The following causes an exception in the auditing node, but it should be caught and
        # should not propagate
        self.__audit_node._QSPAuditNode__on_audit_assigned({})

        # This causes an auditor id mismatch
        evt = {'args': {'auditor': 'this is not me', 'requestId': 1}}
        self.__audit_node._QSPAuditNode__on_audit_assigned(evt)
        
        # Tests a real auditor id with case mismatch and successful submit
        buggy_contract = resource_uri("DAOBug.sol")
        
        tx_hash = self.__request_audit(buggy_contract, self.__PRICE)
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        auditor_id = self.__audit_node._QSPAuditNode__config.account.upper()
        evt = {'args': {'auditor': auditor_id, 'requestId': 1, 'price': self.__PRICE,
                        'requestor': auditor_id, 'uri': buggy_contract}, 'blockNumber': 1}
        self.__audit_node._QSPAuditNode__on_audit_assigned(evt)
        sql3lite_worker = self.__config.event_pool_manager.sql3lite_worker
        row = get_first(sql3lite_worker.execute("select * from audit_evt"))
        self.assertEqual(row['fk_status'], 'AS')

    @timeout(20, timeout_exception=StopIteration)
    def test_on_report_submitted(self):
        # The following causes an exception in the auditing node, but it should be caught and
        # should not propagate
        self.__audit_node._QSPAuditNode__on_report_submitted({})

        # This causes an auditor id mismatch
        evt = {'args': {'auditor': 'this is not me', 'requestId': 1}}
        self.__audit_node._QSPAuditNode__on_report_submitted(evt)
        
        # Tests a real auditor id with case mismatch and successful submit
        buggy_contract = resource_uri("DAOBug.sol")

        tx_hash = self.__request_audit(buggy_contract, self.__PRICE)
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        sql3lite_worker = self.__config.event_pool_manager.sql3lite_worker
        
        while True:
            row = get_first(sql3lite_worker.execute("select * from audit_evt where request_id = 1"))
            if row != {}:
                sql3lite_worker.execute(
                    "update audit_evt set fk_status = 'AS' where request_id = 1")
                break
            else:
                sleep(1)
        auditor_id = self.__audit_node._QSPAuditNode__config.account.upper()
        evt = {'args': {'auditor': auditor_id, 'requestId': 1}}
        self.__audit_node._QSPAuditNode__on_report_submitted(evt)
        row = get_first(sql3lite_worker.execute("select * from audit_evt where request_id = 1"))
        self.assertEqual(row['fk_status'], 'DN')

    @timeout(80, timeout_exception=StopIteration)
    def test_contract_audit_request(self):
        """
        Tests the entire flow of a successful audit request, from a request
        to the production of a report and its submission.
        """
        # since we're mocking the smart contract, we should explicitly call its internals
        buggy_contract = resource_uri("DAOBug.sol")
        tx_hash = self.__request_audit(buggy_contract, self.__PRICE)
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(1)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__evt_wait_loop(self.__submitReport_filter)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(0)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        # NOTE: if the audit node later requires the stubbed fields, this will have to change a bit

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__send_done_message(self.__REQUEST_ID)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(0)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__assert_audit_request(self.__REQUEST_ID, self.__AUDIT_STATE_SUCCESS,
                                    "reports/DAOBug.json")
        self.__assert_all_analyzers(self.__REQUEST_ID)

    @timeout(80, timeout_exception=StopIteration)
    def test_contract_audit_request_not_in_db(self):
        """
        Tests the entire flow of a successful audit request when the request is not stored in the
        database prior to being assigned.
        """
        # since we're mocking the smart contract, we should explicitly call its internals
        worker = Sqlite3Worker(self.__config.logger, self.__config.evt_db_path)
        buggy_contract = resource_uri("DAOBug.sol")

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(1)
        self.__audit_node._QSPAuditNode__web3_unlock()
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__assign_audit(self.__REQUEST_ID, buggy_contract, self.__PRICE, 0)
        self.__audit_node._QSPAuditNode__web3_unlock()
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__assign_audit(12, buggy_contract, self.__PRICE, 0)
        self.__audit_node._QSPAuditNode__web3_unlock()
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__assign_audit(9, buggy_contract, self.__PRICE, 0)
        self.__audit_node._QSPAuditNode__web3_unlock()
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
            
        result = worker.execute("select * from audit_evt")
        while len(result) != 3:
            sleep(1)
            result = worker.execute("select * from audit_evt")
        ids = [x['request_id'] for x in result]
        self.assertTrue(self.__REQUEST_ID in ids)
        self.assertTrue(9 in ids)
        self.assertTrue(12 in ids)

        self.__evt_wait_loop(self.__submitReport_filter)

        # NOTE: if the audit node later requires the stubbed fields, this will have to change a bit
        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__send_done_message(self.__REQUEST_ID)
        self.__audit_node._QSPAuditNode__web3_unlock()
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(0)
        self.__audit_node._QSPAuditNode__web3_unlock()
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__assert_audit_request(self.__REQUEST_ID, self.__AUDIT_STATE_SUCCESS,
                                    "reports/DAOBug.json")
        self.__assert_all_analyzers(self.__REQUEST_ID)

    @timeout(80, timeout_exception=StopIteration)
    def test_buggy_contract_audit_request(self):
        """
        Tests the entire flow of an erroneous audit request, from a request
        to the production of a report and its submission.
        """
        buggy_contract = resource_uri("BasicToken.sol")
        tx_hash = self.__request_audit(buggy_contract, self.__PRICE)
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(1)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        self.__evt_wait_loop(self.__submitReport_filter)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(0)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        # NOTE: if the audit node later requires the stubbed fields, this will have to change a bit
        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__send_done_message(self.__REQUEST_ID)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__assert_audit_request(self.__REQUEST_ID, self.__AUDIT_STATE_ERROR, "reports/BasicToken.json")

    
    @timeout(80, timeout_exception=StopIteration)
    def test_target_contract_in_non_raw_text_file(self):
        """
        Tests the entire flow of an audit request of a non-raw text file contract (e.g., HTML), from a request
        to the production of a report and its submission.
        """
        buggy_contract = resource_uri("DappBinWallet.sol")

        print("===> 1")
        
        tx_hash = self.__request_audit(buggy_contract, self.__PRICE)
        print("===> tx_hash is " + str(tx_hash))
        

        print("===> 2")

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        print("===> tx_hash is " + str(tx_hash))

        print("===> 3")

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(1)
        self.__audit_node._QSPAuditNode__web3_unlock()

        print("===> 4")

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)        
        self.__evt_wait_loop(self.__submitReport_filter)

        print("===> 5")

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(0)
        self.__audit_node._QSPAuditNode__web3_unlock()

        print("===> 6")

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        

        print("===> 7")

        # NOTE: if the audit node later requires the stubbed fields, this will have to change a bit
        self.__audit_node._QSPAuditNode__web3_lock()
        self.__config.web3_client.eth.waitForTransactionReceipt(
            self.__send_done_message(self.__REQUEST_ID)
        )
        self.__audit_node._QSPAuditNode__web3_unlock()

        print("===> 8")

        self.__assert_audit_request(self.__REQUEST_ID, self.__AUDIT_STATE_ERROR,
                                    "reports/DappBinWallet.json")

        print("===> 8")

    @timeout(80, timeout_exception=StopIteration)
    def test_analyzer_produces_metadata_for_errors(self):
        """
        Tests that analyzers produce their metadata even when failure occurs
        """
        buggy_contract = resource_uri("BasicToken.sol")
        buggy_contract_file = fetch_file(buggy_contract)
        # directly calling this function to avoid compilation checks;
        # this will cause error states for the analyzers
        report = self.__audit_node.get_audit_report_from_analyzers(buggy_contract_file,
                                                                   "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf",
                                                                   buggy_contract,
                                                                   1)
        self.__compare_json(report, "reports/BasicTokenErrorWithMetadata.json", json_loaded=True)

    @timeout(5, timeout_exception=StopIteration)
    def test_run_audit_evt_thread(self):
        """
        Tests that the run_evt_thread dies upon an internal exception
        """
        # An exception should be re-thrown inside the thread
        thread = self.__audit_node._QSPAuditNode__run_audit_evt_thread(None, None, None, True)
        thread.join()
        self.assertFalse(thread.is_alive(), "Thread was supposed be terminated by an exception")

    # def test_run_multiple_instances_expecting_fail(self):
    #     """
    #     Tests that a second instance of the node cannot be started
    #     """
    #     thrown = True
    #     try:
    #         self.__audit_node.run()
    #         thrown = False
    #     except Exception:
    #         # the exception is too generic to use self.fail
    #         pass
    #     self.assertTrue(thrown, "No exception was thrown when starting multiple instances")

    # # Variable to be passed to the mocked function
    # __mocked__get_next_audit_request_called = False

    # @timeout(40, timeout_exception=StopIteration)
    # def test_restricting_local_max_assigned(self):
    #     """
    #     Tests if the limitation on the local maximum assigned requests is in effect
    #     """

    #     # Mocking the QSPAuditNode.__get_next_audit_request. This function is supposed to be called if
    #     # the limit is not reached.
    #     original__get_next_audit_request = self.__audit_node._QSPAuditNode__get_next_audit_request

    #     def mocked__get_next_audit_request():
    #         # this should be unreachable when the limit is reached
    #         self.__mocked__get_next_audit_request_called = True

    #     self.assertEqual(int(self.__config.max_assigned_requests), 1)

    #     # Make sure there anyAvailableRequest returns ready state
    #     self.__config.web3_client.eth.waitForTransactionReceipt(
    #         self.__config.audit_contract.functions.setAnyRequestAvailableResult(
    #             self.__AVAILABLE_AUDIT__STATE_READY).transact(
    #             {"from": self.__config.account})
    #     )
    #     self.__evt_wait_loop(self.__setAnyRequestAvailableResult_filter)

    #     self.__evt_wait_loop(self.__getNextAuditRequest_filter)

    #     buggy_contract = resource_uri("DappBinWallet.sol")
    #     self.__config.web3_client.eth.waitForTransactionReceipt(
    #         self.__send_request_message(self.__REQUEST_ID, buggy_contract, self.__PRICE, 100)
    #     )

    #     self.__config.web3_client.eth.waitForTransactionReceipt(self.__set_assigned_request_count(1))

    #     # Node should not ask for further request
    #     self.__audit_node._QSPAuditNode__get_next_audit_request = mocked__get_next_audit_request

    #     # Make sure there is enough time for mining poll to call QSPAuditNode.__check_then_bid_audit
    #     # request
    #     sleep(self.__config.block_mined_polling + 1)

    #     self.__evt_wait_loop(self.__submitReport_filter)

    #     self.__config.web3_client.eth.waitForTransactionReceipt(self.__set_assigned_request_count(0))

    #     self.__config.web3_client.eth.waitForTransactionReceipt(
    #         self.__send_done_message(self.__REQUEST_ID))

    #     # Restore QSPAuditNode.__get_next_audit_request actual implementation
    #     self.__audit_node._QSPAuditNode__get_next_audit_request = original__get_next_audit_request

    #     # This is a critical line to be called as the node did all it audits
    #     self.__evt_wait_loop(self.__getNextAuditRequest_filter)

    #     # an extra call to get_next_audit is no accepted
    #     self.assertFalse(self.__mocked__get_next_audit_request_called)

    @timeout(30, timeout_exception=StopIteration)
    def test_timeout_on_complex_file(self):
        """
        Tests if the analyzer throttles the execution and generates error message
        """

        # rewiring configs
        original_timeouts = []
        for i in range(0, len(self.__config.analyzers)):
            # It's an expected behaviour
            analyzer_name = self.__config.analyzers[i].wrapper.analyzer_name
            self.assertEqual(
                self.__audit_node._QSPAuditNode__config.analyzers[i].wrapper._Wrapper__timeout_sec,
                self.__audit_node._QSPAuditNode__config._Config__analyzers_config[i][analyzer_name][
                    'timeout_sec'])
            original_timeouts.append(
                self.__audit_node._QSPAuditNode__config.analyzers[i].wrapper._Wrapper__timeout_sec)
            self.__audit_node._QSPAuditNode__config.analyzers[i].wrapper._Wrapper__timeout_sec = 6
            self.__audit_node._QSPAuditNode__config._Config__analyzers_config[i][analyzer_name][
                'timeout_sec'] = 3

        contract = resource_uri("kyber.sol")
        tx_hash = self.__request_audit(contract, self.__PRICE)
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(1)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__evt_wait_loop(self.__submitReport_filter)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__set_assigned_request_count(0)
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__send_done_message(self.__REQUEST_ID)
        self.__audit_node._QSPAuditNode__web3_unlock()
        
        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)

        self.__assert_audit_request(self.__REQUEST_ID, self.__AUDIT_STATE_ERROR,
                                    "reports/kyber.json")

        # setting back the configurations
        for i in range(0, len(original_timeouts)):
            self.__audit_node._QSPAuditNode__config.analyzers[i].wrapper._Wrapper__timeout_sec = \
                original_timeouts[i]
            analyzer_name = self.__config.analyzers[i].wrapper.analyzer_name
            self.__audit_node._QSPAuditNode__config._Config__analyzers_config[i][analyzer_name][
                'timeout_sec'] = \
                original_timeouts[i]

    @timeout(30, timeout_exception=StopIteration)
    def test_change_min_price(self):
        """
        Tests that the node updates the min_price on the blockchain if the config value changes
        """
        self.__audit_node._QSPAuditNode__config._Config__min_price_in_qsp = 1

        self.__audit_node._QSPAuditNode__web3_lock()
        self.__audit_node._QSPAuditNode__check_and_update_min_price()
        self.__audit_node._QSPAuditNode__web3_unlock()

        events = self.__evt_wait_loop(self.__setAuditNodePrice_filter)
        self.assertEqual(events[0]['args']['price'], 1000000000000000000)
        self.assertEqual(events[0]['event'], 'setAuditNodePrice_called')

    @timeout(10, timeout_exception=StopIteration)
    def test_not_whitelisted(self):
        # the address has to have fixed length, do not truncate
        address = '0x0000000000000000000000000000000000000000'
        self.assertFalse(self.__audit_node._QSPAuditNode__check_whitelist(address),
                         "Address 0x0 is not whitelisted")

    @timeout(10, timeout_exception=StopIteration)
    def test_not_whitelisted_run(self):
        self.__audit_node.stop()
        # the address has to have fixed length, do not truncate
        address = '0x0000000000000000000000000000000000000000'
        self.__audit_node.config._Config__account = address
        try:
            self.__audit_node.run()
            self.fail("This should throw an error")
        except ExecutionException:
            pass

    @timeout(30, timeout_exception=StopIteration)
    def test_configuration_checks(self):
        """
        Tests configuration sanity checks.
        Since this test requires loading the QuantstampAuditData contract, it is better here than test_config.py.
        """
        config = self.__audit_node._QSPAuditNode__config
        config_utils = ConfigUtils(config.node_version)
        try:
            temp = config.submission_timeout_limit_blocks
            config._Config__submission_timeout_limit_blocks = 2
            config_utils.check_configuration_settings(config)
            self.fail("Configuration error should have been raised.")
        except ConfigurationException:
            config._Config__submission_timeout_limit_blocks = temp
        try:
            temp = config.submission_timeout_limit_blocks
            config._Config__submission_timeout_limit_blocks = 123
            config_utils.check_configuration_settings(config)
            self.fail("Configuration error should have been raised.")
        except ConfigurationException:
            config._Config__submission_timeout_limit_blocks = temp
        for i in range(0, len(self.__audit_node._QSPAuditNode__config.analyzers)):
            try:
                temp = self.__audit_node._QSPAuditNode__config.analyzers[i].wrapper._Wrapper__timeout_sec
                self.__audit_node._QSPAuditNode__config.analyzers[i].wrapper._Wrapper__timeout_sec = 123456
                config_utils.check_configuration_settings(config)
                self.fail("Configuration error should have been raised.")
            except ConfigurationException:
                self.__audit_node._QSPAuditNode__config.analyzers[i].wrapper._Wrapper__timeout_sec = temp

    def __assert_audit_request(self, request_id, expected_audit_state, report_file_path):
        sql3lite_worker = self.__config.event_pool_manager.sql3lite_worker

        # Busy waits on receiving events up to the configured
        # timeout (60s)
        row = None

        while True:
            row = get_first(
                sql3lite_worker.execute("select * from audit_evt where fk_status = 'DN'"))
            if row != {} and row['request_id'] == request_id:
                break
            else:
                sleep(1)
        self.assertEqual(row['evt_name'], "LogAuditAssigned")

        # FIXME: add range validation
        self.assertTrue(int(row['block_nbr']) > 0)

        self.assertEqual(int(row['price']), 100)
        self.assertEqual(row['submission_attempts'], 1)
        self.assertEqual(row['is_persisted'], True)

        self.assertTrue(row['tx_hash'] is not None)
        self.assertTrue(row['contract_uri'] is not None)

        audit_uri = row['audit_uri']
        audit_state = row['audit_state']
        audit_file = fetch_file(audit_uri)
        self.assertEqual(digest_file(audit_file), row['audit_hash'])
        self.assertEqual(audit_state, expected_audit_state)
        self.__compare_json(audit_file, report_file_path)

    def __assert_all_analyzers(self, request_id):
        """
        Asserts that all configured analyzers were executed and are included in the report.
        """
        sql3lite_worker = self.__config.event_pool_manager.sql3lite_worker

        # Busy waits on receiving events up to the configured
        # timeout (60s)
        row = None
        while True:
            row = get_first(
                sql3lite_worker.execute("select * from audit_evt where fk_status = 'DN'"))
            if row != {} and row['request_id'] == request_id:
                break
            else:
                sleep(1)

        audit_uri = row['audit_uri']
        audit_file = fetch_file(audit_uri)
        actual_json = load_json(audit_file)
        executed_analyzers = [x['analyzer']['name'] for x in actual_json['analyzers_reports']]
        for analyzer in self.__config.analyzers_config:
            name, conf = list(analyzer.items())[0]
            self.assertTrue(name in executed_analyzers)

    def __compare_json(self, audit_file, report_file_path, json_loaded=False):
        if not json_loaded:
            actual_json = load_json(audit_file)
        else:
            actual_json = audit_file
        expected_json = load_json(fetch_file(resource_uri(report_file_path)))
        diff = DeepDiff(actual_json,
                        expected_json,
                        exclude_paths={
                            "root['contract_uri']",
                            # path is different depending on whether running inside Docker
                            "root['timestamp']",
                            "root['start_time']",
                            "root['end_time']",
                            "root['analyzers_reports'][0]['coverages'][0]['file']",
                            "root['analyzers_reports'][0]['potential_vulnerabilities'][0]['file']",
                            "root['analyzers_reports'][0]['start_time']",
                            "root['analyzers_reports'][0]['end_time']",
                            "root['analyzers_reports'][0]['hash']",
                            "root['analyzers_reports'][1]['analyzer']['command']",
                            "root['analyzers_reports'][1]['coverages'][0]['file']",
                            "root['analyzers_reports'][1]['potential_vulnerabilities'][0]['file']",
                            "root['analyzers_reports'][1]['start_time']",
                            "root['analyzers_reports'][1]['end_time']",
                            "root['analyzers_reports'][1]['hash']",
                        }
                        )
        pprint(diff)
        self.assertEqual(diff, {})
        self.assertEqual(ntpath.basename(actual_json['contract_uri']),
                         ntpath.basename(expected_json['contract_uri']))

    def __evt_wait_loop(self, current_filter):
        wait = True
        while wait:
            events = current_filter.get_new_entries()
            for evt in events:
                wait = False
                break
            sleep(1)
        return events

    def __request_audit(self, contract_uri, price):
        """
        Emulates requesting for a new audit.
        """
        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__config.audit_contract.functions.setAnyRequestAvailableResult(
            self.__AVAILABLE_AUDIT__STATE_READY).transact({"from": self.__config.account})
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        self.__evt_wait_loop(self.__setAnyRequestAvailableResult_filter)
        self.__evt_wait_loop(self.__getNextAuditRequest_filter)

        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash = self.__config.audit_contract.functions.setAnyRequestAvailableResult(
                self.__AVAILABLE_AUDIT__STATE_ERROR).transact({"from": self.__config.account})
        self.__audit_node._QSPAuditNode__web3_unlock()

        self.__config.web3_client.eth.waitForTransactionReceipt(tx_hash)
        
        self.__evt_wait_loop(self.__setAnyRequestAvailableResult_filter)

        timestamp = 40

        return self.__send_request_message(
            self.__REQUEST_ID,
            contract_uri,
            price,
            timestamp)

    def __send_request_message(self, request_id, contract_uri, price, timestamp):
        """
        Emulates assigning a request for a given target contract by submitting the appropriate event.
        """
        requester = self.__config.account
        auditor = self.__config.account
        self.__audit_node._QSPAuditNode__web3_lock()
        tx_hash =  self.__config.audit_contract.functions.emitLogAuditAssigned(
            request_id,
            requester,
            auditor,
            contract_uri,
            price,
            timestamp).transact({"from": requester})
        self.__audit_node._QSPAuditNode__web3_unlock()
    
        return tx_hash

    def __send_done_message(self, request_id):
        return self.__config.audit_contract.functions.emitLogAuditFinished(
            request_id,
            self.__config.account,
            0,
            "",
            0).transact({"from": self.__config.account})

    def __set_assigned_request_count(self, num):
        return self.__config.audit_contract.functions.setAssignedRequestCount(self.__config.account,
                                                                              num).transact(
            {"from": self.__config.account})

    def __assign_audit(self, request_id, uri, price, timestamp):
        return self.__config.audit_contract.functions.emitLogAuditAssigned(request_id,
                                                                           self.__config.account,
                                                                           self.__config.account,
                                                                           uri, price,
                                                                           timestamp).transact(
            {"from": self.__config.account})


if __name__ == '__main__':
    unittest.main()
