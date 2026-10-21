import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import requests
from requests_auth_aws_sigv4 import AWSSigV4

os.environ["TERM"] = "xterm"


BASE_URL = "https://platform.dev.myob-edops.com"
AWS_SERVICE = "execute-api"
AWS_REGION = "ap-southeast-2"
AWS_CREDENTIALS_FILE = "aws.credentials"

session = boto3.Session(profile_name="myob-ed-development")
credentials = session.get_credentials()
auth = AWSSigV4(
    region=AWS_REGION,
    service=AWS_SERVICE,
    access_id=credentials.access_key,
    secret_key=credentials.secret_key,
    session_token=credentials.token,
)


def refresh_server(id, enableHyperThreading=None):
    """Refresh server by triggering failover API

    Args:
        id (_type_): Server ID
        enableHyperThreading (_type_, optional): Enable Hyper-Threading. Defaults to None.
    """
    base = f"{BASE_URL}/api/hosting/v1/webserver/failover?webServerId={id}&isRefreshServer=true"
    if enableHyperThreading is not None:
        url = f"{base}&enableHyperThreading={enableHyperThreading}"
    else:
        url = base
    call_api(url, method="PUT")


def check_completed(id: int) -> dict:
    """Check if the server failover has completed

    Args:
        id (int): Server ID

    Returns:
        dict: Server failover status
    """
    url = f"{BASE_URL}/api/hosting/v1/webserver/{id}?getFailoverInfo=true"

    try:
        data = call_api(url, method="GET")
        return {
            "Id": data.get("id", id),
            "InstanceId": data.get("instanceId"),
            "Name": data.get("name"),
            "Sites": data.get("sites"),
            "Status": data.get("status", "FailingOver"),
            "EnableHyperthreading": data.get("enableHyperthreading"),
            "LastFailoverStatus": data.get("lastFailoverStatus")
            or {
                "TotalServerFailoverSites": None,
                "TotalMigratedSite": None,
                "TotalMigratingFailedSite": None,
                "TotalMigratingSite": None,
            },
        }

    except Exception as _:
        return {
            "Id": id,
            "InstanceId": None,
            "Name": f"Server-{id}",
            "Status": "FailingOverFailed",
            "EnableHyperthreading": None,
            "LastFailoverStatus": {
                "TotalServerFailoverSites": None,
                "TotalMigratedSite": None,
                "TotalMigratingFailedSite": None,
                "TotalMigratingSite": None,
            },
        }


def call_api(url, method="GET", data=None):
    """Call API with AWS SigV4 authentication

    Args:
        url (str): API endpoint URL
        method (str, optional): HTTP method. Defaults to "GET".
        data (dict, optional): Data to send with the request. Defaults to None.

    Raises:
        ValueError: If an unsupported HTTP method is provided

    Returns:
        dict: JSON response from the API
    """
    try:
        if method == "GET":
            response = requests.get(url, auth=auth)
        elif method == "POST":
            response = requests.post(url, json=data, auth=auth)
        elif method == "PUT":
            response = requests.put(url, json=data, auth=auth)
        elif method == "DELETE":
            response = requests.delete(url, auth=auth)
        else:
            raise ValueError("Unsupported HTTP method")

        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error calling {url}:", e)


def healthcheck_site(site_name):
    """Check the health of a specific site

    Args:
        site_name (str): Name of the site

    Returns:
        bool: True if the site is healthy, False otherwise
    """
    url = f"{BASE_URL}/api/hosting/v1/website/health-check/{site_name}?isAlert=false&isSimpleCheck=false"
    result = call_api(url, method="GET")
    if result is None:
        return False
    return result.get("isHealthy", False)


def check_server_health(sites):
    """Check the health of multiple sites

    Args:
        sites (list): List of site dictionaries

    Returns:
        bool: True if all sites are healthy, False otherwise
    """
    if not sites:
        return True

    batch_size = 10
    for i in range(0, len(sites), batch_size):
        batch = sites[i : i + batch_size]
        print(f"checking batch {i // batch_size + 1}")

        with ThreadPoolExecutor(max_workers=5) as exe:
            futures = [
                exe.submit(healthcheck_site, s["siteName"]) for s in batch
            ]
            for f in as_completed(futures):
                if not f.result():
                    return False

        time.sleep(1)

    return True


def format_server_row(info, start_time):
    """Format server information into a row for display

    Args:
        info (dict): Server information
        start_time (float): Start time of the server operation

    Returns:
        str: Formatted server row
    """
    name = info.get("Name") or ""
    instance = info.get("InstanceId") or ""
    ht = info.get("EnableHyperthreading")
    status = info.get("Status", "")
    migrated = info.get("TotalMigratedSite")
    has_elapsed = info.get("elapsed", "0")
    total = info.get("TotalServerFailoverSites")
    failed = info.get("TotalMigratingFailedSite")
    healthy = info.get("healthy", "N/A")
    progress = (
        f"{migrated}/{total}"
        if migrated is not None and total is not None
        else "--"
    )

    elapsed = ""
    if info.get("Status") not in ("Deleted", "FailingOverFailed"):
        elapsed_sec = int(time.time() - start_time)
        elapsed = f"{elapsed_sec}s"
    else:
        elapsed = f"{has_elapsed}s"

    return (
        f"{name:<20} "
        f"{instance:<15} "
        f"HT={str(ht):<5} "
        f"Status={status:<18} "
        f"Health={healthy:<10} "
        f"Progress={progress:<8} "
        f"Failed={failed:<5} "
        f"Time={elapsed}"
    )


def refresh_servers_batch(ids):
    """Refresh a batch of servers

    Args:
        ids (list): List of server IDs
    """
    print("Starting refresh ...")

    start_time_all = time.time()

    for id in ids:
        refresh_server(id)

    server_states = {
        id: {
            "done": False,
            "start_time": time.time(),
            "elapsed": None,
            "healthy": "N/A",
        }
        for id in ids
    }

    failed_ids = []

    while True:
        all_done = True
        rows_running = []
        rows_completed = []

        for id in ids:
            state = server_states[id]
            data = check_completed(id)

            if not data:
                continue

            sites = data.get("Sites") or []

            if state["done"] and state["healthy"] == "N/A":
                healthy = check_server_health(sites)
            else:
                healthy = state["healthy"]

            state["healthy"] = healthy

            l = data.get("LastFailoverStatus") or {}
            data["TotalMigratedSite"] = l.get("totalMigratedSite")
            data["TotalServerFailoverSites"] = l.get(
                "totalServerFailoverSites"
            )
            data["TotalMigratingFailedSite"] = l.get(
                "totalMigratingFailedSite"
            )
            data["Healthy"] = healthy

            status = data.get("Status")

            if status in ("Deleted", "FailingOverFailed"):
                elapsed_sec = int(time.time() - state["start_time"])
                state["elapsed"] = elapsed_sec
                state["done"] = True
                if status == "FailingOverFailed" and str(id) not in failed_ids:
                    failed_ids.append(str(id))
                row = format_server_row(data, state["start_time"])
                rows_completed.append(row)
            else:
                all_done = False
                row = format_server_row(data, state["start_time"])
                rows_running.append(row)

        print("\033[2J\033[H")

        print("=== Completed ===")
        for r in rows_completed:
            print(r)

        print("\n=== Running ===")
        for r in rows_running:
            print(r)

        if all_done:
            break

        time.sleep(5)

    total_elapsed = int(time.time() - start_time_all)

    print("\n=== All refresh completed ===")
    print(f"Total time: {total_elapsed}s\n")

    if failed_ids:
        print("Failed IDs:", ",".join(failed_ids))
        retry = input("Retry failed servers? (y/n) ").strip().lower()
        if retry == "y":
            refresh_servers_batch([int(x) for x in failed_ids])


def safe_clear():
    """Clear the terminal screen safely."""
    try:
        if os.isatty(1):
            os.system("cls" if os.name == "nt" else "clear")
    except Exception as _:
        pass


if __name__ == "__main__":
    ids = [
        322,
    ]
    refresh_servers_batch(ids)
