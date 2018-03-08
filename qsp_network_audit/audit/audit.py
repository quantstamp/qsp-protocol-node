"""
Provides the QSP Audit node implementation.
"""
import json
import utils.logging as logging_utils
import traceback

from queue import Queue
from datetime import datetime
from tempfile import mkstemp
from time import sleep
from hashlib import sha256
from utils.io import fetch_file, digest
from utils.eth import mk_args
from threading import Thread

logger = logging_utils.get_logger()


class QSPAuditNode:

    __EVT_AUDIT_REQUESTED = "LogAuditRequested"
    __EVT_AUDIT_REQUEST_ASSIGNED = "LogAuditRequestAssigned"
    __EVT_REPORT_SUBMITTED = "LogReportSubmitted"

    def __init__(self, config):
        """
        Builds a QSPAuditNode object from the given input parameters.
        """
        self.__config = config
        self.__exec = False

        start_block = self.__config.event_pool_manager.get_latest_block_number()

        # If no block has currently been processed, start from zero
        if start_block == -1:
            start_block = 0

        logger.debug(" Filtering events from block # {0}".format(str(start_block)))

        self.__filter_audit_requests = self.__config.internal_contract.on(
            QSPAuditNode.__EVT_AUDIT_REQUESTED,
            filter_params={'fromBlock': start_block},
        )

        self.__filter_audit_assignments = self.__config.internal_contract.on(
            QSPAuditNode.__EVT_AUDIT_REQUEST_ASSIGNED,
            filter_params={'fromBlock': start_block},
        )

        self.__filter_audit_submissions = self.__config.internal_contract.on(
            QSPAuditNode.__EVT_REPORT_SUBMITTED,
            filter_params={'fromBlock': start_block},
        )

        self.__latest_request_id = self.__config.event_pool_manager.get_latest_request_id()

        self.__threads = []

    @property
    def config(self):
        return self.__config

    def run(self):
        """
        Starts all the threads processing different stages of a given event.
        """
        self.__exec = True

        self.__run_audit_bid_thread()
        self.__run_audit_assignment_thread()
        self.__run_perform_audit_thread()
        self.__run_submission_thread()
        self.__run_monitor_submisson_thread()

    def __run_filter_polling(self, thread_name, filter, process_evt, only_changes=False):
        def exec():
            logger.debug("Started polling")

            while self.__exec:
                evts = filter.get(only_changes)
                for evt in evts:
                    request_id = int(evt['args']['requestId'])

                    if self.__config.event_pool_manager.is_request_processed(request_id):
                        logger.debug(
                            "Ignoring request {0} (already processed).".format(
                                str(request_id)
                            )
                        )
                    else:
                        process_evt(evt)

                sleep(self.__config.evt_polling)

        polling_thread = Thread(target=exec, name=thread_name)
        self.__threads.append(polling_thread)
        polling_thread.start()

    def __run_audit_bid_thread(self):
        def bid_for_audit_request(evt):
            """
            Bids for an audit upon an audit request event.
            """
            try:
                # Bids for audit requests whose reward is at least as
                # high as given by the configured min_price
                price = evt['args']['price']
                request_id = str(evt['args']['requestId'])

                if price >= self.__config.min_price:
                    logger.debug("Accepted processing audit event: {0}. Bidding for it".format(
                        str(evt),
                        requestId=request_id,
                    ))
                    self.__get_next_audit_request()

                else:
                    logger.debug(
                        "Declining processing audit request: {0}. Not enough incentive".format(
                            str(evt)
                        ),
                        requestId=request_id,
                    )
            except Exception as error:
                logger.exception(
                    "Error when bidding for request {0}: {1}".format(str(evt), str(error)), 
                    requestId=request_id,
                )

        self.__run_filter_polling(
            "bidding thread",
            self.__filter_audit_requests,
            bid_for_audit_request,
            self.__latest_request_id,
        )

    def __run_audit_assignment_thread(self):
        def process_audit_assignment(evt):
            request_id = str(evt['args']['requestId'])
            try:
                target_auditor = evt['args']['auditor']

                # If an audit request is not targeted to the
                # running audit node, just disconsider it
                if target_auditor.lower() != self.__config.account.lower():
                    logger.debug(
                        "Ignoring audit request (not directed at current node): {0}".format(
                            str(evt)
                        ),
                        requestId=request_id,
                    )
                    pass

                logger.debug(
                    "Saving audit request for processing: {0}".format(
                        str(evt)
                    ),
                    requestId=request_id,
                )

                # Otherwise, the audit request must be processed
                # throught its different stages. As such, save it
                # in the internal database, marking it as RECEIVED

                audit_evt = {
                    'request_id': request_id,
                    'requestor': str(evt['args']['requestor']),
                    'contract_uri': str(evt['args']['uri']),
                    'evt_name':  QSPAuditNode.__EVT_AUDIT_REQUEST_ASSIGNED,
                    'block_nbr': evt['blockNumber'],
                    'price': evt['args']['price'],
                    'status_info': "Audit request received",
                }

                self.__config.event_pool_manager.add_evt_to_be_processed(
                    audit_evt
                )
            except Exception as error:
                logger.exception(
                    "Error when processing event {0}: {1}".format(str(evt), str(error)), 
                    requestId=request_id,
                )

        self.__run_filter_polling(
            "filter assignments thread",
            self.__filter_audit_assignments,
            process_audit_assignment,
            self.__latest_request_id,
        )

    def __run_perform_audit_thread(self):
        def process_audit_request(evt):
            try:
                requestor = evt['requestor']
                request_id = evt['request_id']
                contract_uri = evt['contract_uri']
                report = self.audit(requestor, contract_uri, request_id)

                if report is None:
                    error = "Could not generate report"
                    evt['status_info'] = error
                    logging.exception(error, requestId=request_id)
                    self.__config.event_pool_manager.set_evt_to_error(evt)
                else:
                    evt['report'] = json.dumps(report)
                    evt['status_info'] = "Sucessfully generated report"
                    logging.debug(
                        "Generated report is {0}. Saving it in the internal database".format(
                            str(evt['report']),
                            requestId=request_id,
                        )
                    )
                    self.__config.event_pool_manager.set_evt_to_be_submitted(evt)
            except Exception:
                logging.exception(
                    "Unexpected error when performing audit", 
                    requestId=request_id,
                )
                evt['status_info'] = traceback.format_exc()
                self.__config.event_pool_manager.set_evt_to_error(evt)
                pass

        def exec():
            while self.__exec:
                self.__config.event_pool_manager.process_incoming_events(
                    process_audit_request
                )
                sleep(self.__config.evt_polling)

        audit_thread = Thread(target=exec, name="audit thread")
        self.__threads.append(audit_thread)
        audit_thread.start()

    def __run_submission_thread(self):
        def process_submission_request(evt):
            try:
                tx_hash = self.__submit_report(
                    int(evt['request_id']),
                    evt['requestor'],
                    evt['contract_uri'],
                    evt['report'],
                )
                evt['tx_hash'] = tx_hash
                evt['status_info'] = 'Report submitted (waiting for confirmation)'
                self.__config.event_pool_manager.set_evt_to_submitted(evt)
            except Exception:
                evt['status_info'] = traceback.format_exc()
                self.__config.event_pool_manager.set_evt_to_error(evt)

        def exec():
            while self.__exec:
                self.__config.event_pool_manager.process_events_to_be_submitted(
                    process_submission_request
                )

                sleep(self.__config.evt_polling)

        submission_thread = Thread(target=exec, name="submission thread")
        self.__threads.append(submission_thread)
        submission_thread.start()


    def __run_monitor_submisson_thread(self):
        timeout_limit = self.__config.submission_timeout_limit_blocks

        def monitor_submission_timeout(evt, current_block):
            if (current_block - evt['block_nbr']) > timeout_limit:
                evt['status_info'] = "Submission timeout"
                self.__config.event_pool_manager.set_evt_to_error(evt)

            # TODO How to inform the network of a submission timeout?

        def exec():
            while self.__exec:
                evts = self.__filter_audit_submissions.get()

                # Processes the current event batch
                if evts != []:
                    for evt in evts:
                        request_id = str(evt['args']['requestId'])
                        audit_evt = self.__config.event_pool_manager.get_event_by_request_id(
                            request_id
                        )
                        if audit_evt is not None:
                            audit_evt['status_info'] = 'Report successfully submitted'
                            self.__config.event_pool_manager.set_evt_to_done(
                                audit_evt
                            )

                # Checks for a potential timeouts
                block = self.__config.web3_client.eth.blockNumber
                self.__config.event_pool_manager.process_submission_events(
                    monitor_submission_timeout,
                    block,
                )

                sleep(self.__config.evt_polling)

        monitor_thread = Thread(target=exec, name="monitor thread")
        self.__threads.append(monitor_thread)
        monitor_thread.start()

    def stop(self):
        """
        Signals to the executing QSP audit node that is should stop the execution of the node.
        """
        self.__exec = False
        for thread in self.__threads:
            thread.join()

        self.__threads = []

        # Close resources
        self.__config.wallet_session_manager.lock()
        self.__config.event_pool_manager.close()

    def audit(self, requestor, uri, request_id):
        """
        Audits a target contract.
        """
        logging.info(
            "Executing audit on contract at {0}".format(uri), 
            requestId=request_id,
        )

        target_contract = fetch_file(uri)

        report = self.__config.analyzer.check(
            target_contract,
            self.__config.analyzer_output,
            request_id,
        )
        
        report_as_string = str(json.dumps(report))
        
        upload_result = self.__config.report_uploader.upload(report_as_string)
        logging.info(
            "Report upload result: {0}".format(upload_result),
            requestId=request_id,
        )
        
        if (upload_result['success'] is False):
          raise Exception("Unexpected error when uploading report: {0}".format(
              json.dumps(upload_result)),
              requestId=request_id,
            )

        report_as_string = str(json.dumps(report))

        upload_result = self.__config.report_uploader.upload(report_as_string)

        logging.info(
            "Report upload result: {0}".format(upload_result), 
            requestId=request_id,
        )

        if not upload_result['success']:
            raise Exception("Error uploading report: {0}".format(json.dumps(upload_result)))

        return {
            'auditor': self.__config.account,
            'requestor': str(requestor),
            'contract_uri': str(uri),
            'contract_sha256': str(digest(target_contract)),
            'report_uri': upload_result['url'],
            'report_sha256': sha256(report_as_string.encode()).hexdigest(),
            'timestamp': str(datetime.utcnow()),
        }

    def __get_next_audit_request(self):
        """
        Attempts to get a request from the audit request queue.
        """
        tx_args = mk_args(self.__config)
        self.__config.wallet_session_manager.unlock(self.__config.account_ttl)
        return self.__config.internal_contract.transact(tx_args).getNextAuditRequest() 

    def __submit_report(self, request_id, requestor, contract_uri, report):
        """
        Submits the audit report to the entire QSP network.
        """
        tx_args = mk_args(self.__config)
        self.__config.wallet_session_manager.unlock(self.__config.account_ttl)
        return self.__config.internal_contract.transact(tx_args).submitReport(
            request_id,
            requestor,
            contract_uri,
            report,
        )
