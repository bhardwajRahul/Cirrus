#!/usr/bin/env python3
"""
GCP Cloud Shell script to automate the setup and cleanup of a Google Cloud environment in preparation for and
resulting from, use of the Mirage Google Cloud Platform (GCP) and Google Workspaces (GW) collectors.

Inspired by the Google project "create-service-account", the 'setup' subcommand of the script automates the steps
required for obtaining a service account key. How this script affects a Google environment will vary depending
on whether GCP, GW, or both Google services are being investigated. The 'cleanup' subcommand automates the steps to
remove all traces of activity from 'setup' and evidence collection.
"""

import argparse
import asyncio
import datetime
import json
import logging
import os
import re
import sys
import time
import traceback
import urllib.parse

from google.auth.exceptions import RefreshError
from google.oauth2 import service_account
from google_auth_httplib2 import Request
from httplib2 import Http

# CHANGE ME
PROJECT_NAME = "sir"  # Name of project created in GCP environment (datetime appended). 1-10 lowercase letters and/or
# numbers!

# Tool & Validation constants
SERVICE_ACCT_NAME = f"{PROJECT_NAME}-service-account"  # Name of service account generated in project
VERSION = "1"
TOOL_NAME = "mirage_assistant"
TOOL_NAME_FRIENDLY = "Mirage Assistant"
USER_AGENT = f"create_service_account_v{VERSION}"
PROJECT_NAME_PATT = r'^[a-z0-9]{1,10}$'

# Stylistic color constants
BG = "\u001b[32;1m"  # Bright green
GD = "\u001b[33;5;220m"  # Gold
RR = "\u001b[0m"  # Reset

# File constants
RUNNING_DIRECTORY = os.path.realpath(__file__).rpartition('/')[0]
DEFAULT_OUTPUT_FOLDER = os.path.join(RUNNING_DIRECTORY, 'reference')
ID_FILE = os.path.join(DEFAULT_OUTPUT_FOLDER, 'project_id')
ROLE_BINDINGS_FILE = os.path.join(DEFAULT_OUTPUT_FOLDER, 'role_bindings_tracker')
UNDELETED_ID_FILE = os.path.join(RUNNING_DIRECTORY, 'undeleted_project_id')
UNDELETED_ROLE_BINDINGS_FILE = os.path.join(RUNNING_DIRECTORY, 'undeleted_role_bindings')
TROUBLESHOOTING_LOG_FILE = 'mirage_assistant.log'
TROUBLESHOOTING_LOG_FILE_PATH = os.path.join(RUNNING_DIRECTORY, f'{TROUBLESHOOTING_LOG_FILE}')
KEY_FILE = os.path.join(RUNNING_DIRECTORY, f"{TOOL_NAME.lower()}-service-account-key-"
                                           f"{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.json")

# API and scope constants
DWD_URL_FORMAT = ("https://admin.google.com/ac/owl/domainwidedelegation?"
                  "overwriteClientId=true&clientIdToAdd={}&clientScopeToAdd={}")
GOOGLE_CLOUD_APIS = ["admin.googleapis.com",
                     "gmail.googleapis.com",
                     "cloudasset.googleapis.com"]
SCOPES_ALL = ['https://www.googleapis.com/auth/admin.directory.user.readonly',
              'https://www.googleapis.com/auth/admin.directory.domain.readonly',
              'https://www.googleapis.com/auth/admin.directory.user.security',
              'https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly',
              'https://www.googleapis.com/auth/admin.directory.customer.readonly',
              'https://www.googleapis.com/auth/admin.directory.group.readonly',
              'https://www.googleapis.com/auth/admin.directory.device.mobile.readonly',
              'https://www.googleapis.com/auth/admin.directory.orgunit.readonly',
              'https://www.googleapis.com/auth/admin.directory.rolemanagement.readonly',
              'https://www.googleapis.com/auth/admin.reports.audit.readonly',
              'https://www.googleapis.com/auth/admin.reports.usage.readonly',
              'https://www.googleapis.com/auth/gmail.readonly']

# Mapping constants
SUPPORTED_MODULES = ['logs', 'configurations', 'all']
PROFILE = ["roles/logging.privateLogViewer", "roles/cloudasset.viewer"]


def change_me_section_check() -> None:
    """Validates the project name in the "change me" section that the user might change. 
    If the validation fails, the script exists with error code 1"""
    if not re.match(PROJECT_NAME_PATT, PROJECT_NAME):
        logging.critical("project name must be no longer than 10 letters or numbers! Update the \"CHANGE ME\" "
                         "section in the script file and change the \"PROJECT NAME\" accordingly")
        sys.exit(1)


def get_arguments() -> (argparse.ArgumentParser, argparse.Namespace):
    """Creates the argparse and processes the command-line arguments"""
    parser = argparse.ArgumentParser('mirage_assistant.py',
                                     description='Prepare a Google Cloud environment for incident response.')
    mode_subparser = parser.add_subparsers(dest='mode',
                                           help='specify the Google Cloud environment configuration mode',
                                           required=True)
    setup_subparser = mode_subparser.add_parser('setup')
    cleanup_subparser = mode_subparser.add_parser('cleanup')
    # SETUP subparser
    setup_subparser.add_argument('--service', type=str, default=None,
                                 help='specify a Google Cloud service to begin forensic artifact collection against: '
                                      '[gcp, gw, all]')
    setup_subparser.add_argument('--project-id', type=str, default=None,
                                 help='in comma-delimited format (no spaces), specify project ID(s) to perform setup '
                                      'actions against')
    setup_subparser.add_argument('--folder-id', type=str, default=None,
                                 help='in comma-delimited format (no spaces), specify folder ID(s) to perform setup '
                                      'actions against')
    setup_subparser.add_argument('--organization-id', type=str, default=None,
                                 help='specify organization ID to perform setup actions against')
    # CLEANUP subparser
    cleanup_subparser.add_argument('--service', type=str, default=None,
                                   help='specify a Google Cloud service for removal of artifacts resulting from the '
                                        'script\'s setup functionality: [gcp, gw, all]')
    return parser, parser.parse_args()


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Validates argparse command-line arguments

    @param parser: the Argparse object
    @param args: the parsed args as given by parser.parse_args()
    """

    if args.service not in ['gcp', 'gw', 'all']:
        parser.error("specify one option with the '--service' flag: [gcp, gw, all]")
    if args.service in ['gcp', 'all'] and args.mode == 'setup':
        if args.project_id is None and args.folder_id is None and args.organization_id is None:
            parser.error("specify at least one resource type with the corresponding resource ID(s): "
                         "[--project-id ID1,ID2...] [--folder-id ID1,ID2...] [--organization-id ID]")
        if (args.project_id or args.folder_id) and args.organization_id:
            parser.error("specify a single organization ID OR multiple project and folder ID(s)")


async def retryable_command(command: str,
                            max_num_retries=3,
                            retry_delay=5,
                            suppress_errors=False,
                            require_output=False) -> (bytes, bytes, int):
    """
    Executes a given command several times with delays and returns the stdout, stderr, and return code

    @param command: the command to execute in Google Cloud Shell
    @param max_num_retries: the maximum number of attempts to execute the command
    @param retry_delay: how many seconds to sleep between each attempt
    @param suppress_errors: Whether to supress errors or not
    @param require_output: Whether to return the output of the command or not
    """

    num_tries = 1
    while num_tries <= max_num_retries:
        logging.debug("Executing command (attempt %d): %s", num_tries, command)
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        return_code = process.returncode

        logging.debug("stdout: %s", stdout.decode())
        logging.debug("stderr: %s", stderr.decode())
        logging.debug("Return code: %d", return_code)

        if return_code == 0:
            if not require_output or (require_output and stdout):
                return stdout, stderr, return_code

        if num_tries < max_num_retries:
            num_tries += 1
            await asyncio.sleep(retry_delay)
        elif suppress_errors:
            return stdout, stderr, return_code
        else:
            logging.critical("Failed to execute command: `%s`", stderr.decode())
            sys.exit(return_code)


def create_reference_folder() -> None:
    """Creates folder for reference file output if it does not already exist"""
    try:
        os.makedirs(DEFAULT_OUTPUT_FOLDER, exist_ok=True)
    except Exception as e:
        logging.info(f"Cannot create reference folder [{DEFAULT_OUTPUT_FOLDER}] due "
                     f"to the following error: {str(e)}")


async def _check_project_creation() -> None:
    """Checks if script has already been executed and a project was successfully created accordingly. 
    If the user refuses to continue, the script exists with error code 0"""
    if os.path.exists(ID_FILE):
        gcp_user_response = input(
            f"A '{ID_FILE}' file has already been generated, which means the setup script has "
            f"previously been executed successfully.\nIf you wish to make additional IAM role "
            f"bindings to a previously created service account, specify the '--iam-append' argument with "
            f"the 'gcp' subparser. If you continue with execution, a new project and service account will be "
            f"created in the targeted environment.\n\nPress Enter to continue or 'n' to exit: ")
        if gcp_user_response.lower() == "n":
            sys.exit(0)
        else:
            os.remove(f"{ID_FILE}")


async def create_project() -> None:
    """Creates a new project in GCP"""
    logging.info(f"Creating project...")
    project_id = f"{PROJECT_NAME.lower()}-{int(time.time() * 1000)}"
    project_name = f"{PROJECT_NAME.lower()}-{datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"
    await retryable_command(f"gcloud projects create {project_id} "
                            f"--name {project_name} --set-as-default")
    with open(ID_FILE, 'a') as f:
        f.write(project_id + "\n")
    logging.info(f"Project [{project_id}] created successfully \u2705")


async def verify_tos_accepted() -> None:
    """Checks for the first API if it can be enabled and enables it.
    If it can't be done, the user is requested to accept the Terms of service"""
    logging.info(f"Verifying acceptance of Terms of service...")
    tos_accepted = False
    while not tos_accepted:
        command = f"gcloud services enable {GOOGLE_CLOUD_APIS[0]}"
        _, stderr, return_code = await retryable_command(
            command, max_num_retries=1, suppress_errors=True)
        if return_code:
            err_str = stderr.decode()
            if "UREQ_TOS_NOT_ACCEPTED" in err_str:
                if "universal" in err_str:
                    logging.debug("Google APIs Terms of Service not accepted")
                    print(f"You must first accept the Google APIs Terms of Service. You "
                          "can accept the terms of service by clicking "
                          "https://console.developers.google.com/terms/universal and "
                          "clicking 'Accept'.\n")
                elif "appsadmin" in err_str:
                    logging.debug("Google Apps Admin APIs Terms of Service not accepted")
                    print(f"You must first accept the Google Apps Admin APIs Terms of "
                          "Service. You can accept the terms of service by clicking "
                          "https://console.developers.google.com/terms/appsadmin and "
                          "clicking 'Accept'.\n")
                answer = input(f"If you've accepted the terms of service, press Enter "
                               "to try again or 'n' to cancel:")
                if answer.lower() == "n":
                    sys.exit(0)
            else:
                logging.critical(err_str)
                sys.exit(1)
        else:
            tos_accepted = True
    logging.info(f"Terms of service acceptance verified \u2705")


async def verify_service_account_authorization() -> None:
    """Verifies all scopes are authorized. """
    logging.info(f"Verifying service account authorization...")
    admin_user_email = await _get_admin_user_email()
    service_account_id = await _get_service_account_id()
    scopes_are_authorized = False
    while not scopes_are_authorized:
        scope_authorization_failures = []
        for scope in SCOPES_ALL:
            scope_authorized = _verify_scope_authorization(admin_user_email, scope)
            if not scope_authorized:
                scope_authorization_failures.append(scope)
        if scope_authorization_failures:
            scopes = urllib.parse.quote(",".join(SCOPES_ALL), safe="")
            authorize_url = DWD_URL_FORMAT.format(service_account_id, scopes)
            logging.info(f"The service account is not properly authorized.")
            logging.warning("The following scopes are missing:")
            for scope in scope_authorization_failures:
                logging.warning("\t- %s", scope)
            print(f"\nTo fix this, please click the following link. After clicking "
                  "'Authorize', return here to try again. If you are confident "
                  "that these scopes have already been added, then you may continue "
                  "now. If you encounter OAuth errors in the tool, then you may need "
                  "to wait for the appends to propagate. Propagation generally takes "
                  "less than 1 hour. However, in rare cases, it can take up to 24 hours.")
            print(f"\n{authorize_url}\n")
            answer = input("Press Enter to try again, 'c' to continue, or 'n' to "
                           "cancel:")
            if answer.lower == "c":
                scopes_are_authorized = True
            if answer.lower() == "n":
                sys.exit(0)
        else:
            scopes_are_authorized = True
    logging.info(f"Service account successfully authorized \u2705")


async def _get_project_id() -> str:
    """Gets a project id"""
    command = "gcloud config get-value project"
    project_id, _, _ = await retryable_command(command, require_output=True)
    return project_id.decode().rstrip()


async def _get_admin_user_email() -> str:
    """Gets the gcloud admin account email"""
    command = 'gcloud auth list --format="value(account)"'
    admin_user_email, _, _ = await retryable_command(command, require_output=True)
    return admin_user_email.decode().rstrip()


async def authorize_service_account_dwd() -> None:
    """Requests the user to authorize domain wide delegations for the scopes"""
    service_account_id = await _get_service_account_id()
    scopes = urllib.parse.quote(",".join(SCOPES_ALL), safe="")
    authorize_url = DWD_URL_FORMAT.format(service_account_id, scopes)
    input(f"\nBefore using {TOOL_NAME_FRIENDLY}, you must authorize the service "
          "account to perform actions on behalf of your users. You can do so by "
          f"clicking:\n\n{authorize_url}\n\nAfter clicking 'Authorize', return "
          "here and press Enter to continue.")


def _verify_scope_authorization(subject: str, scope: str) -> bool:
    """Verifies that the subject account can be delegated with the scopes and handles relevant exceptions
    
    @param subject: the user email to check for scope authorization
    @param scope: the OAuth2 scope (permissions) that needs to be verified against the subject
    """

    try:
        _get_access_token_for_scopes(subject, [scope])
        return True
    except RefreshError:
        return False
    except Exception as e:
        logging.error(f"An unknown error occurred: {str(e)}")
        return False


def _get_access_token_for_scopes(subject: str, scopes: list):
    """Obtains a token for the delegated credentials of subject by the scopes

    @param subject: the user email to check for scope authorization
    @param scopes: the OAuth2 scopes (permissions)

    """

    logging.debug(f"Getting access token for scopes {scopes}, user {subject} ...")
    credentials = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=scopes)
    delegated_credentials = credentials.with_subject(subject)
    request = Request(Http())
    delegated_credentials.refresh(request)
    logging.debug(f"Access token obtained successfully \u2705")
    return delegated_credentials.token


async def _get_service_account_id() -> str:
    """Gets the service account id; this function can be executed only after a project is set in gcloud"""
    command = 'gcloud iam service-accounts list --format="value(uniqueId)"'
    service_account_id, _, _ = await retryable_command(
        command, require_output=True)
    return service_account_id.decode().rstrip()


async def create_service_account() -> None:
    """Creates the service account"""
    logging.info(f"Creating service account ...")
    service_account_name = f"{SERVICE_ACCT_NAME}"
    await retryable_command(f"gcloud iam service-accounts create {service_account_name}")
    service_account_email = await _get_service_account_email()
    logging.info(f"Service account [{service_account_email}] created successfully \u2705")


async def _enable_api(api) -> None:
    """Enables a single API that is passed to the function

    @param api: the Google API to enable
    """

    command = f"gcloud services enable {api}"
    await retryable_command(command)


async def enable_apis() -> None:
    """Enables APIs in preparation for evidence collection from GW/CI and GCP"""
    logging.info(f"Enabling APIs ...")
    # verify_tos_accepted checks the first API, so skip it here.
    enable_api_calls = map(_enable_api, GOOGLE_CLOUD_APIS[1:])
    await asyncio.gather(*enable_api_calls)
    logging.info(f"APIs enabled successfully \u2705")


async def verify_api_access() -> None:
    """Verifies all APIs are accessible"""
    logging.info(f"Verifying API access...")
    admin_user_email = await _get_admin_user_email()
    project_id = await _get_project_id()
    token = _get_access_token_for_scopes(admin_user_email, SCOPES_ALL)
    retry_api_verification = True
    while retry_api_verification:
        disabled_apis = {}
        disabled_services = []
        retry_api_verification = False
        for api in GOOGLE_CLOUD_APIS:
            api_name = service_name = ""
            raw_api_response = ""
            if api == "admin.googleapis.com":
                # Admin SDK does not have a corresponding service.
                api_name = "Admin SDK"
                raw_api_response = execute_api_request(
                    f"https://content-admin.googleapis.com/admin/directory/v1/users/{admin_user_email}?fields=isAdmin",
                    token)
            if api == "calendar-json.googleapis.com":
                api_name = service_name = "Calendar"
                raw_api_response = execute_api_request(
                    "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=1&fields=kind",
                    token)
            if api == "contacts.googleapis.com":
                # Contacts does not have a corresponding service.
                api_name = "Contacts"
                raw_api_response = execute_api_request(
                    "https://www.google.com/m8/feeds/contacts/a.com/full/invalid_contact",
                    token)
            if api == "drive.googleapis.com":
                api_name = service_name = "Drive"
                raw_api_response = execute_api_request(
                    "https://www.googleapis.com/drive/v3/files?pageSize=1&fields=kind", token)
            if api == "gmail.googleapis.com":
                api_name = service_name = "Gmail"
                raw_api_response = execute_api_request(
                    "https://gmail.googleapis.com/gmail/v1/users/me/labels?fields=labels.id", token)
            if api == "tasks.googleapis.com":
                api_name = service_name = "Tasks"
                raw_api_response = execute_api_request(
                    "https://tasks.googleapis.com/tasks/v1/users/@me/lists?maxResults=1&fields=kind", token)
            if api == "cloudasset.googleapis.com":
                api_name = service_name = "CloudAsset"
                raw_api_response = execute_api_request(
                    f"https://cloudasset.googleapis.com/v1/projects/{project_id}/assets", token)

            if _is_api_disabled(raw_api_response):
                disabled_apis[api_name] = api
                retry_api_verification = True

            if service_name and _is_service_disabled(raw_api_response):
                disabled_services.append(service_name)
                retry_api_verification = True

        if disabled_apis:
            disabled_api_message = (
                "The {} API is not enabled. Please enable it by clicking "
                "https://console.developers.google.com/apis/api/{}/overview?project={}."
            )
            for api_name in disabled_apis:
                api_id = disabled_apis[api_name]
                print(disabled_api_message.format(api_name, api_id, project_id))
            print(f"\nIf these APIs are already enabled, then you may need to wait "
                  "for the appends to propagate. Propagation generally takes a few "
                  "minutes. However, in rare cases, it can take up to 24 hours.\n")

        if not disabled_apis and disabled_services:
            disabled_service_message = "The {0} service is not enabled for {1}."
            for service in disabled_services:
                print(disabled_service_message.format(service, admin_user_email))
            print(f"\nIf this is expected, then please continue. If this is not "
                  "expected, then please ensure that these services are enabled for "
                  "your users by visiting "
                  "https://admin.google.com/ac/appslist/core.\n")

        if retry_api_verification:
            answer = input("Press Enter to try again, 'c' to continue, or 'n' to "
                           "cancel:")
            if answer.lower() == "c":
                retry_api_verification = False
            if answer.lower() == "n":
                sys.exit(0)

    logging.info(f"API access verified \u2705")


def _is_api_disabled(raw_api_response: str) -> bool:
    """Checks if a given API HTTPS response is empty or that is has an error message embedded in the results

    @param raw_api_response: the raw API response. Can be either None or str (in a json format)
    """

    if not raw_api_response:
        return True
    try:
        api_response = json.loads(raw_api_response)
        if "error" in api_response:
            return "it is disabled" in api_response["error"]["message"]
    except Exception as e:
        logging.error(f'general exception in API disable state check: {str(e)}. Raw response: {raw_api_response}')
        pass
    return False


def _is_service_disabled(raw_api_response: str) -> bool:
    """Checks if a given service HTTPS response is empty or that is has an error message embedded in the results

    @param raw_api_response: the raw API response. Can be either None or str (in a json format)
    """

    if not raw_api_response:
        return True
    try:
        api_response = json.loads(raw_api_response)
        if "error" in api_response and "errors" in api_response["error"]:
            error_reason = api_response["error"]["errors"][0]["reason"]
            if "notACalendarUser" or "notFound" or "authError" in error_reason:
                return True
    except:
        pass

    try:
        api_response = json.loads(raw_api_response)
        if "error" in api_response and "message" in api_response["error"]:
            if "service not enabled" in api_response["error"]["message"]:
                return True
    except:
        pass

    return False


def execute_api_request(url: str, token: str) -> object:
    """Executes an API request to a given url with a token

    @param url: the URL matching the relevant API request
    @param token: the OAuth2 token
    @return: Either the str representation of the response or None if the response is empty
    """

    try:
        http = Http()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT
        }
        logging.debug("Executing API request %s", url)
        _, content = http.request(url, "GET", headers=headers)
        decoded_content = content.decode()
        logging.debug("Response: %s", decoded_content)
        return decoded_content
    except:
        e = sys.exc_info()[0]
        logging.error("Failed to execute API request: %s", e)
        return None


async def create_service_account_key() -> None:
    """Creates the key for the service account"""
    logging.info(f"Creating service account key ...")
    service_account_email = await _get_service_account_email()
    await retryable_command(f"gcloud iam service-accounts keys create {KEY_FILE} "
                            f"--iam-account={service_account_email}")
    logging.info(f"Service account key created successfully \u2705")


async def download_service_account_key() -> None:
    """Downloads the service account key"""
    command = f"cloudshell download {KEY_FILE}"
    await retryable_command(command)


async def delete_key() -> None:
    """Deletes the key from cloud shell after it has been downloaded"""
    input(f"\nPress Enter after you have downloaded the file, as it is about to be shredded.")
    logging.debug(f"Deleting key file ${KEY_FILE}...")
    command = f"shred -u {KEY_FILE}"
    await retryable_command(command)


async def _get_service_account_email() -> str:
    """Gets the service account email; this function can be executed only after a project is set in gcloud"""
    command = 'gcloud iam service-accounts list --format="value(email)"'
    service_account_email, _, _ = await retryable_command(
        command, require_output=True)
    return service_account_email.decode().rstrip()


async def assign_role_binding(project_resource_ids: list = None, folder_resource_ids: list = None,
                              org_resource_id: list = None) -> None:
    """Used to assign IAM role bindings in preparation for GCP forensic collection script functionality.
    The function works independently on each param list, therefore ignored lists should be None.

    @param project_resource_ids: a list of the project resource ids
    @param folder_resource_ids: a list of the folder resource ids
    @param org_resource_id: a list of the organization resource ids
    """
    service_account_email = await _get_service_account_email()
    logging.info(f"Beginning of role binding assignment ...")

    # Role bindings for project(s)
    if project_resource_ids is not None:
        for project_id in project_resource_ids:
            await assign_single_role_binding('project', project_id, service_account_email)

    # Role bindings for folder(s)
    if folder_resource_ids is not None:
        for folder_id in folder_resource_ids:
            await assign_single_role_binding('folder', folder_id, service_account_email)

    # Role bindings for organization
    if org_resource_id is not None:
        for org_id in org_resource_id:
            await assign_single_role_binding('organization', org_id, service_account_email)

    # Final completion message
    logging.info(f"End of role binding assignment \u2705")
    logging.info(f"Role bindings are tracked in [{ROLE_BINDINGS_FILE}]")


async def assign_single_role_binding(resource_type, resource_id, service_account_email) -> None:
    """
    Assigns a single role binding to a resource id and an identity entity (service_account_email) based 
    on its type. The assigned roles are: ["roles/logging.privateLogViewer", "roles/cloudasset.viewer"]
    
    @param resource_type: the type of the resource (e.g., organization)
    @param resource_id: the resource id to associate the role binding with
    @param service_account_email: the email address to associate the role binding with
    """

    gcloud_commands = {
        'project': f"gcloud projects add-iam-policy-binding {resource_id} "
                   f"--member=serviceAccount:{service_account_email} --role=",
        'folder': f"gcloud resource-manager folders add-iam-policy-binding {resource_id} "
                  f"--member=serviceAccount:{service_account_email} --role=",
        'organization': f"gcloud organizations add-iam-policy-binding {resource_id} "
                        f"--member=serviceAccount:{service_account_email} --role="
    }
    for role in PROFILE:
        # Validation and skip action if role binding already exists
        if os.path.isfile(ROLE_BINDINGS_FILE):
            if role_binding_check(resource_type, resource_id, role, service_account_email):
                logging.debug(f"The '{role}' role in the [{resource_id}] project "
                              f"has already been bound to [{service_account_email}] ... skipping role binding.")
                continue
        # Continue with role binding action if not skipped via previous validation
        logging.info(f"Assigning '{role}' to resource [{resource_id}] ...")
        command = gcloud_commands[resource_type] + role
        _, stderr, return_code = await retryable_command(command,
                                                         max_num_retries=1,
                                                         suppress_errors=True)
        # Error checking to see if previous resource ID(s) were entered incorrectly
        err_str = stderr.decode()
        if "may not exist" in err_str:
            logging.debug(f"The specified resource ID does not exist or exists outside the scope "
                          f"of the client's organization: [{resource_id}]")
            print(f"The specified resource ID does not exist or exists "
                  f"outside the scope of the targeted organization: [{resource_id}]\n"
                  f"Please re-run the script with the correct folder ID.")
            sys.exit(0)
        # If role binding succeeds, it is logged into ROLE_BINDINGS_FILE for tracking (and deletion)
        with open(ROLE_BINDINGS_FILE, 'a') as fh:
            fh.write(f"{resource_type},{resource_id},{role},{service_account_email}\n")
        # Final completion message
        logging.info(f"Role binding successful \u2705")


def role_binding_check(resource: str, resource_id: str, role: str, service_account_email: str) -> bool:
    """Used to check if role binding has been previously recorded in the cache file

    @param resource: the type of the resource (e.g., organization)
    @param resource_id: the resource id to associate the role binding with
    @param role: the role to be assigned
    @param service_account_email: the email address to associate the role binding with
    """

    role_binding = f"{resource},{resource_id},{role},{service_account_email}"
    with open(ROLE_BINDINGS_FILE, 'r') as f:
        return role_binding in f.read()


def check_project_requirements() -> bool:
    """Check if the 'project_id' file is located in the appropriate location"""
    return os.path.exists(ID_FILE)


def read_projects() -> list:
    """Read project ID(s) in preparation for project deletion from cache file"""
    project_ids = []
    with open(ID_FILE, 'r') as f:
        for line in f:
            project_ids.append(line.rstrip())
    return project_ids


async def delete_projects(project_ids) -> int:
    """Deletes all project IDs from GCP; returns how many projects were deleted successfully

    @param project_ids: the project ids to delete
    """

    problematic_ids = []
    count_deleted = 0
    for project_id in project_ids:
        successfully_deleted = await delete_project(project_id)
        if not successfully_deleted:
            problematic_ids.append(project_id)
        else:
            count_deleted += 1
    try:
        os.remove(ID_FILE)
    except Exception as e:
        logging.info(f"Cannot locate/delete file: [{ID_FILE}] \u274c")
        logging.debug(f"{str(e)}")
    try:
        os.rmdir(DEFAULT_OUTPUT_FOLDER)
    except Exception as e:
        logging.info(f"Cannot locate/delete folder: [{DEFAULT_OUTPUT_FOLDER}] \u274c")
        logging.debug(f"{str(e)}")
    for problematic_id in problematic_ids:
        with open(UNDELETED_ID_FILE, 'a') as f:
            f.write(problematic_id + "\n")
    if len(problematic_ids) > 0:
        logging.info(f"{len(problematic_ids)} projects could not be deleted and can be found in "
                     f"the following file: [{UNDELETED_ID_FILE}]")
    return count_deleted


async def delete_project(project_id) -> bool:
    """Deletes a single project in GCP; returns whether the project was deleted successfully

    @param project_id: the project id to delete
    """

    logging.info(f"Deleting project [{project_id}] ...")
    try:
        await retryable_command(f"gcloud projects delete {project_id} -q")
        logging.info(f"Project deletion successful \u2705")
        return True
    except Exception as e:
        logging.info(f"Project deletion failure \u274c")
        logging.debug(f"{str(e)}")
        return False


def check_gcp_requirements() -> bool:
    """Check if the 'role_bindings_tracker' file is located in the appropriate location"""
    return os.path.exists(ROLE_BINDINGS_FILE)


def read_role_bindings() -> list:
    """Read role bindings in preparation for removal from cache file"""
    role_bindings = []
    with open(ROLE_BINDINGS_FILE, 'r') as fh:
        for line in fh:
            role_bindings.append(line.rstrip())
    return role_bindings


async def remove_role_bindings(role_bindings: list) -> (int, int):
    """Removes all or specified role bindings from GCP; returns how many role bindings were deleted successfully
    and how many had problems.

    @param role_bindings: a list of comma-seperated strings that each represents the role_binding:
    resource, resource_id, role, service_account_email
    """

    logging.info(f"Beginning of role binding removal ...")
    problematic_role_bindings = []
    count_deleted = 0
    problem_count = 0

    # Remove role bindings
    for role_binding in role_bindings:
        resource, resource_id, role, service_account_email = role_binding.split(',')
        successfully_deleted = await remove_role_binding(resource, role, resource_id, service_account_email)
        if successfully_deleted:
            count_deleted += 1
        else:
            problematic_role_bindings.append(role_binding)

    # Delete entire role bindings tracker file
    try:
        os.remove(ROLE_BINDINGS_FILE)
    except Exception as e:
        logging.debug(f"{str(e)}")
        logging.info(f"Cannot locate/delete file: [{ROLE_BINDINGS_FILE}] \u274c")

    # Regardless of condition, number of failed role binding deletion attempts is tracked for reporting
    for problematic_role_binding in problematic_role_bindings:
        with open(UNDELETED_ROLE_BINDINGS_FILE, 'a') as fh:
            fh.write(problematic_role_binding + "\n")
    if len(problematic_role_bindings) > 0:
        logging.info(f"{len(problematic_role_bindings)} role bindings could not be removed.")
        logging.info(f"Undeleted role bindings tracked in [{UNDELETED_ROLE_BINDINGS_FILE}]")

    # Return counts for additional tracking
    logging.info(f"End of role binding removal \u2705")
    return count_deleted, problem_count


async def remove_role_binding(resource_type, role, resource_id, service_account_email) -> bool:
    """Removes a single role binding in GCP; returns whether the role binding was deleted successfully.

    @param resource_type: the resource type (e.g., organization)
    @param role: the role to be associated
    @param resource_id: the resource id
    @param service_account_email: the identity entity email address
    """

    logging.info(f"Removing '{role}' assigned to service account in {resource_type} [{resource_id}] ...")
    gcloud_commands = {
        'project': f"gcloud projects remove-iam-policy-binding {resource_id} "
                   f"--member=serviceAccount:{service_account_email} --role={role}",
        'folder': f"gcloud resource-manager folders remove-iam-policy-binding {resource_id} "
                  f"--member=serviceAccount:{service_account_email} --role={role}",
        'organization': f"gcloud organizations add-iam-policy-binding {resource_id} "
                        f"--member=serviceAccount:{service_account_email} --role={role}"
    }
    try:
        command = gcloud_commands[resource_type]
        await retryable_command(command, max_num_retries=1)
        logging.info(f"Role binding removal successful \u2705")
        return True
    except Exception as e:
        logging.info(f"Role binding removal failure \u274c")
        logging.debug(f"{str(e)}")
        return False


async def get_service_account_id_cleanup() -> str:
    """Gathers the service account email based on the 'project_id' reference file"""
    # Ensure that 'project_id' file reference is available from executing 'setup' functionality
    try:
        with open(ID_FILE, 'r') as fh:
            isolated_project_id = fh.read().rstrip()
    except Exception as e:
        logging.info(f"Ensure the 'project_id' file generated from the initial execution of the "
                     "setup functionality of the script has been placed into the references directory.")
        logging.debug(f"{str(e)}")
    # Set project as isolated project created with setup execution
    command = f"gcloud config set project {isolated_project_id}"
    await retryable_command(command, max_num_retries=1, suppress_errors=True)
    # Obtain service account email from isolated project
    command = "gcloud iam service-accounts list --format='value(uniqueId)'"
    service_account_email, _, _ = await retryable_command(command, require_output=True)
    return service_account_email.decode().rstrip()


def init_logger(args: argparse.Namespace) -> None:
    """Initialize logger for DEBUG and INFO levels; DEBUG messages outputs are saved to log file

    @param args: the command line arguments after being parsed from argparse
    """
    logging.basicConfig(
        filename=f"{TROUBLESHOOTING_LOG_FILE}",
        format="[%(asctime)s][%(levelname)s] %(message)s",
        datefmt="%FT%TZ",
        level=logging.DEBUG)
    # Log INFO level messages and above to the console
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter(f"[{GD}{args.mode}{RR}:{BG}{args.service}{RR}] %(message)s")
    console.setFormatter(formatter)
    logging.getLogger("").addHandler(console)


async def start_setup(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Starts the setup required for Mirage. The setup mainly involves the following steps:
    1. creates a project to work in
    2. verify terms of service are accepted
    3. enables relevant apis
    4. creates a service account
    5. authorizes the service in domain wide delegations [Google Workspace only]
    6. creates a service account key
    7. verifies the service account authorization
    8. verifies api access
    9. assigns role binding to relevant resources [GCP only]
    10. downloads the service account key
    11. shreds the key from the Google Cloud Shell console

    @param parser: the argparse object
    @param args: the argparse arguments after being parsed
    """

    create_reference_folder()
    try:
        # Check if script has already been run successfully and if so, give option to quit
        await _check_project_creation()
        project_resource_ids = None
        folder_resource_ids = None
        org_resource_id = None

        # Variable setup in preparation for role binding assignment
        if args.service == 'gcp' or args.service == 'all':
            if args.project_id:
                project_resource_ids = [x for x in args.project_id.split(',')]
            if args.folder_id:
                folder_resource_ids = [x for x in args.folder_id.split(',')]
            if args.organization_id:
                org_resource_id = [x for x in args.organization_id.split(',')]
                if len(org_resource_id) > 1:
                    parser.error("you may only specify a single organization ID")

        # Console prompt
        os.system("clear")
        response = input(
            f"Welcome! This script will create and authorize the "
            "resources necessary for Google Cloud incident response. "
            f"The following steps will be performed on your behalf:\n\n"
            "1. creates a project to work in\n"
            "2. verify terms of service are accepted\n"
            "3. enables relevant apis\n"
            "4. creates a service account\n"
            "5. authorizes the service in domain wide delegations [Google Workspace only]\n"
            "6. creates a service account key\n"
            "7. verifies the service account authorization\n"
            "8. verifies api access\n"
            "9. assigns role binding to relevant resources [GCP only]\n"
            "10. downloads the service account key\n"
            "11. shreds the key from the Google Cloud Shell console\n"
            "When the script has completed, you will be prompted to download the service account key. "
            f"This key can then be used for {TOOL_NAME}.\n"
            f"If you have any questions, please speak to an appropriate representative.\n\n"
            f"Press Enter to continue or 'n' to exit: ")
        if response.lower() == "n":
            sys.exit(0)

        await create_project()
        await verify_tos_accepted()
        await enable_apis()
        await create_service_account()
        if args.service == 'gw' or args.service == 'all':
            await authorize_service_account_dwd()
        await create_service_account_key()
        if args.service == 'gw' or args.service == 'all':
            await verify_service_account_authorization()
            await verify_api_access()
        if args.service == 'gcp' or args.service == 'all':
            await assign_role_binding(project_resource_ids,
                                      folder_resource_ids,
                                      org_resource_id)
        await download_service_account_key()
        await delete_key()

        logging.info(f"Done \u2705")
        print(f"\nIf you have already downloaded the file, then you may close this "
              "page. Please remember that this file is highly sensitive. Any person "
              "who gains access to the key file will then have full access to all "
              "resources to which the service account has access. You should treat "
              "it just like you would a password.")

    # General exception catcher
    except (Exception, SystemExit) as e:
        if str(e) != '0':  # Good system exit
            logging.debug(traceback.format_exc())
            print(f"ERROR, please see log [{TROUBLESHOOTING_LOG_FILE_PATH}] for troubleshooting.")


async def start_cleanup(args):
    """Starts the cleanup process after Mirage is no longer needed. The setup mainly involves the following steps

    @param args: the argparse arguments after being parsed
    """
    try:
        os.system("clear")
        response = input(
            f"Welcome! This script will delete the GCP project and role bindings that "
            f"were created during the setup script functionality.\n"
            f"All settings defined for the creation script will be erased.\n"
            f"The deletion of any project in GCP, including this one, can be canceled in a 7 "
            f"days period.\n"
            f"If you have any questions, please speak to an appropriate representative.\n\n"
            f"Press Enter to continue or 'n' to exit:")
        if response.lower() == "n":
            sys.exit(0)

        # Check if 'project_id' file in reference folder
        if not check_project_requirements():
            print(f"\nThe '{ID_FILE}' file must be found in the reference folder "
                  f"[{DEFAULT_OUTPUT_FOLDER}], and include the content of the creation script.")
            sys.exit(1)

        if args.service == 'gcp' or args.service == 'all':

            # Check if 'role_bindings_tracker' file in reference folder
            if not check_gcp_requirements():
                print(f"\nThe '{ROLE_BINDINGS_FILE}' file must be found in the reference folder "
                      f"[{DEFAULT_OUTPUT_FOLDER}], and include the content of the creation script.")
                sys.exit(1)

            # Deletion of role bindings
            role_bindings = read_role_bindings()
            await remove_role_bindings(role_bindings)

        # Deletion of projects
        project_ids = read_projects()
        await delete_projects(project_ids)

    # General exception catcher
    except (Exception, SystemExit) as e:
        if str(e) != '0':  # Good system exit
            logging.debug(traceback.format_exc())
            print(f'ERROR, please see [{TROUBLESHOOTING_LOG_FILE_PATH}] for troubleshooting.')


async def main():
    parser, args = get_arguments()
    init_logger(args)
    change_me_section_check()
    validate_arguments(parser, args)
    # Setup subparser functionality
    if args.mode == 'setup':
        await start_setup(parser, args)
    # Cleanup subparser functionality
    if args.mode == 'cleanup':
        await start_cleanup(args)


if __name__ == "__main__":
    asyncio.run(main())
