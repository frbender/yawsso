import copy
import json
import os
from configparser import NoSectionError
from datetime import datetime, timezone

from yawsso import TRACE, Constant, logger, utils as u

aws_bin = "aws"  # assume `aws` command avail in PATH and is v2. otherwise, allow mutation with -b flag
profiles = None
aws_sso_cache_path = u.xu(os.getenv("AWS_SSO_CACHE_PATH", Constant.AWS_SSO_CACHE_PATH.value))
aws_config_file = u.xu(os.getenv("AWS_CONFIG_FILE", Constant.AWS_CONFIG_FILE.value))
aws_shared_credentials_file = u.xu(os.getenv("AWS_SHARED_CREDENTIALS_FILE", Constant.AWS_SHARED_CREDENTIALS_FILE.value))
aws_default_region = os.getenv("AWS_DEFAULT_REGION", Constant.AWS_DEFAULT_REGION.value)


def get_aws_cli_v2_sso_cached_login(profile):
    file_paths = u.list_directory(aws_sso_cache_path)
    for file_path in file_paths:
        if not file_path.endswith('.json'):
            logger.log(TRACE, f"Not JSON file, skip: {file_path}")
            continue

        data = u.load_json(file_path)
        if data.get("startUrl") != profile["sso_start_url"]:
            logger.log(TRACE, f"Not equal SSO start url, skip: {file_path}")
            continue
        logger.log(TRACE, f"Using cached SSO login: {file_path}")
        return data


def update_aws_cli_v1_credentials(profile_name, profile, credentials):
    if credentials is None:
        logger.warning(f"No appropriate credentials found for profile '{profile_name}'. "
                       f"Skip syncing it. Use --trace flag to see possible error causes.")
        return

    region = profile.get("region", aws_default_region)
    config = u.read_config(aws_shared_credentials_file)
    if config.has_section(profile_name):
        config.remove_section(profile_name)
    config.add_section(profile_name)
    config.set(profile_name, "region", region)
    config.set(profile_name, "aws_access_key_id", credentials["accessKeyId"])
    config.set(profile_name, "aws_secret_access_key", credentials["secretAccessKey"])
    config.set(profile_name, "aws_session_token", credentials["sessionToken"])
    config.set(profile_name, "aws_security_token", credentials["sessionToken"])
    ts_expires_millisecond = credentials["expiration"]
    dt_utc = str(datetime.utcfromtimestamp(ts_expires_millisecond / 1000.0).isoformat() + '+0000')
    config.set(profile_name, "aws_session_expiration", dt_utc)
    u.write_config(aws_shared_credentials_file, config)

    logger.debug(f"Done syncing AWS CLI v1 credentials using AWS CLI v2 SSO login session for profile `{profile_name}`")


def parse_sso_cached_login_expiry(cached_login):
    # older versions of aws-cli might use non-standard format with `UTC` instead of `Z`
    expires_at = cached_login["expiresAt"].replace('UTC', 'Z')
    datetime_format_in_sso_cached_login = "%Y-%m-%dT%H:%M:%SZ"
    expires_utc = datetime.strptime(expires_at, datetime_format_in_sso_cached_login)
    return expires_utc


def parse_assume_role_credentials_expiry(dt_str):
    datetime_format_in_assume_role_expiration = "%Y-%m-%dT%H:%M:%S+00:00"
    expires_utc = datetime.strptime(dt_str, datetime_format_in_assume_role_expiration)
    return expires_utc


def parse_credentials_file_session_expiry(dt_str):
    datetime_format_in_cred_file_aws_session_expiration = "%Y-%m-%dT%H:%M:%S+0000"  # 2020-06-14T17:13:26+0000
    expires_utc = datetime.strptime(dt_str, datetime_format_in_cred_file_aws_session_expiration)
    return expires_utc


def parse_role_name_from_role_arn(role_arn):
    arr = role_arn.split('/')
    return str(arr[len(arr) - 1]).replace('"', '').replace("'", "")


def append_cli_global_options(cmd: str, profile: dict):
    ca_bundle = profile.get('ca_bundle', None)
    if ca_bundle:
        cmd = f"{cmd} --ca-bundle '{ca_bundle}'"

    logger.log(TRACE, f"COMMAND: {cmd}")
    return cmd


def check_sso_cached_login_expires(profile_name, profile):
    cached_login = get_aws_cli_v2_sso_cached_login(profile)
    if cached_login is None:
        u.halt(f"Can not find valid AWS CLI v2 SSO login cache in {aws_sso_cache_path} for profile {profile_name}.")

    expires_utc = parse_sso_cached_login_expiry(cached_login)

    if datetime.utcnow() > expires_utc:
        u.halt(f"Current cached SSO login is expired since {expires_utc.astimezone().isoformat()}. Try login again.")

    return cached_login


def fetch_credentials(profile_name, profile):
    cached_login = check_sso_cached_login_expires(profile_name, profile)

    cmd_get_role_cred = f"{aws_bin} sso get-role-credentials " \
                        f"--output json " \
                        f"--profile {profile_name} " \
                        f"--region {profile['sso_region']} " \
                        f"--role-name {profile['sso_role_name']} " \
                        f"--account-id {profile['sso_account_id']} " \
                        f"--access-token {cached_login['accessToken']}"

    cmd_get_role_cred = append_cli_global_options(cmd_get_role_cred, profile)

    role_cred_success, role_cred_output = u.invoke(cmd_get_role_cred)

    if not role_cred_success:
        logger.log(TRACE, f"ERROR EXECUTING COMMAND: '{cmd_get_role_cred}'. EXCEPTION: '{role_cred_output}'")
        return

    return json.loads(role_cred_output)['roleCredentials']


def get_role_max_session_duration(profile_name, profile):
    role_name = parse_role_name_from_role_arn(profile['role_arn'])

    cmd_get_role = f"{aws_bin} iam get-role " \
                   f"--output json " \
                   f"--profile {profile_name} " \
                   f"--role-name {role_name} " \
                   f"--region {profile['region']}"

    cmd_get_role = append_cli_global_options(cmd_get_role, profile)

    get_role_success, get_role_output = u.invoke(cmd_get_role)

    if not get_role_success:
        logger.log(TRACE, f"ERROR EXECUTING COMMAND: '{cmd_get_role}'. EXCEPTION: {get_role_output}")
        logger.debug(f"Can not determine role {role_name} maximum session duration. "
                     f"Using default value {Constant.ROLE_CHAINING_DURATION_SECONDS.value} seconds.")
        return Constant.ROLE_CHAINING_DURATION_SECONDS.value

    return json.loads(get_role_output)['Role']['MaxSessionDuration']


def fetch_credentials_with_assume_role(profile_name, profile):
    duration_seconds = get_role_max_session_duration(profile_name, profile)
    if duration_seconds > Constant.ROLE_CHAINING_DURATION_SECONDS.value:
        logger.log(TRACE, f"Role {profile['role_arn']} is configured with max duration `{duration_seconds}` seconds. "
                          f"But AWS SSO service-linked role to assume another role_arn defined in source_profile form "
                          f"`role chaining` (i.e. using a role to assume a second role). Fall back session duration "
                          f"to a maximum of one hour. Well, you can always `yawsso` again when session expired!")
        duration_seconds = Constant.ROLE_CHAINING_DURATION_SECONDS.value

    utc_now_ts = int(datetime.utcnow().replace(tzinfo=timezone.utc).timestamp())
    cmd_assume_role_cred = f"{aws_bin} sts assume-role " \
                           f"--output json " \
                           f"--profile {profile_name} " \
                           f"--role-arn {profile['role_arn']} " \
                           f"--role-session-name yawsso-session-{utc_now_ts} " \
                           f"--duration-seconds {duration_seconds} " \
                           f"--region {profile['region']}"

    cmd_assume_role_cred = append_cli_global_options(cmd_assume_role_cred, profile)

    role_cred_success, role_cred_output = u.invoke(cmd_assume_role_cred)

    if not role_cred_success:
        logger.log(TRACE, f"ERROR EXECUTING COMMAND: `{cmd_assume_role_cred}`. EXCEPTION: {role_cred_output}")
        return

    assume_role_cred = json.loads(role_cred_output)['Credentials']

    cred = {}
    cred.update(accessKeyId=assume_role_cred['AccessKeyId'])
    cred.update(secretAccessKey=assume_role_cred['SecretAccessKey'])
    cred.update(sessionToken=assume_role_cred['SessionToken'])

    expire_utc = parse_assume_role_credentials_expiry(assume_role_cred['Expiration'])
    expire_utc_ts_millisecond = int(expire_utc.replace(tzinfo=timezone.utc).timestamp() * 1000)
    cred.update(expiration=expire_utc_ts_millisecond)

    return copy.deepcopy(cred)


def eager_sync_source_profile(source_profile_name, source_profile):
    if profiles and source_profile_name in profiles:  # it will come in main loop, so no proactive sync required
        return
    config = u.read_config(aws_shared_credentials_file)
    if config.has_section(source_profile_name):
        cred_profile = dict(config.items(source_profile_name))
        session_expires_utc = parse_credentials_file_session_expiry(cred_profile['aws_session_expiration'])
        if datetime.utcnow() > session_expires_utc:
            logger.log(TRACE, f"Eagerly sync source_profile `{source_profile_name}`")
            credentials = fetch_credentials(source_profile_name, source_profile)
            update_aws_cli_v1_credentials(source_profile_name, source_profile, credentials)


def load_profile_from_config(profile_name, config):
    try:
        if profile_name == "default":
            profile_opts = config.items(f"{profile_name}")
        else:
            profile_opts = config.items(f"profile {profile_name}")
        return dict(profile_opts)
    except NoSectionError as e:
        u.halt(e)


def is_sso_profile(profile):
    return {"sso_start_url", "sso_account_id", "sso_role_name", "sso_region"} <= profile.keys()


def is_source_profile(profile):
    return {"source_profile", "role_arn", "region"} <= profile.keys()


def update_profile(profile_name, config, new_profile_name=""):
    profile = load_profile_from_config(profile_name, config)

    if new_profile_name == "":
        new_profile_name = profile_name
        logger.log(TRACE, f"Syncing profile... {profile_name}: {profile}")
    else:
        logger.log(TRACE, f"Syncing profile... {profile_name}->{new_profile_name}: {profile}")

    if is_sso_profile(profile):
        credentials = fetch_credentials(profile_name, profile)

    elif is_source_profile(profile):
        source_profile_name = profile['source_profile']
        source_profile = load_profile_from_config(source_profile_name, config)
        if not is_sso_profile(source_profile):
            logger.warning(f"Your source_profile is not an AWS SSO profile. Skip syncing profile `{profile_name}`")
            return
        check_sso_cached_login_expires(source_profile_name, source_profile)
        eager_sync_source_profile(source_profile_name, source_profile)
        logger.log(TRACE, f"Fetching credentials using assume role for `{profile_name}`")
        credentials = fetch_credentials_with_assume_role(profile_name, profile)

    else:
        logger.warning(f"Not an AWS SSO profile nor no source_profile found. Skip syncing profile `{profile_name}`")
        return

    update_aws_cli_v1_credentials(new_profile_name, profile, credentials)
    return credentials
