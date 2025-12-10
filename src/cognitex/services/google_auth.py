"""Google OAuth2 authentication and token management."""

import json
from pathlib import Path

import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from cognitex.config import get_settings

logger = structlog.get_logger()

# Scopes for Gmail and Calendar access
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_credentials_path() -> Path:
    """Get the path to stored OAuth credentials."""
    settings = get_settings()
    return Path(settings.google_credentials_path)


def get_client_secrets_path() -> Path:
    """Get the path to client secrets file."""
    return Path("data/client_secret.json")


def credentials_to_dict(credentials: Credentials) -> dict:
    """Convert credentials to a dictionary for storage."""
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes) if credentials.scopes else SCOPES,
    }


def save_credentials(credentials: Credentials) -> None:
    """Save credentials to file."""
    creds_path = get_credentials_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)

    with open(creds_path, "w") as f:
        json.dump(credentials_to_dict(credentials), f, indent=2)

    logger.info("Credentials saved", path=str(creds_path))


def load_credentials() -> Credentials | None:
    """Load credentials from file if they exist and are valid."""
    creds_path = get_credentials_path()

    if not creds_path.exists():
        logger.debug("No stored credentials found")
        return None

    try:
        with open(creds_path) as f:
            creds_data = json.load(f)

        credentials = Credentials(
            token=creds_data.get("token"),
            refresh_token=creds_data.get("refresh_token"),
            token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=creds_data.get("client_id"),
            client_secret=creds_data.get("client_secret"),
            scopes=creds_data.get("scopes", SCOPES),
        )

        # Check if credentials are valid
        if credentials.valid:
            logger.debug("Loaded valid credentials")
            return credentials

        # Try to refresh if expired
        if credentials.expired and credentials.refresh_token:
            logger.info("Refreshing expired credentials")
            credentials.refresh(Request())
            save_credentials(credentials)
            return credentials

        logger.warning("Stored credentials are invalid and cannot be refreshed")
        return None

    except Exception as e:
        logger.error("Failed to load credentials", error=str(e))
        return None


def get_google_credentials(force_reauth: bool = False, headless: bool = True) -> Credentials:
    """
    Get valid Google credentials, initiating OAuth flow if necessary.

    Args:
        force_reauth: Force re-authentication even if valid credentials exist
        headless: Use console-based auth flow (no browser required)

    Returns:
        Valid Google credentials
    """
    if not force_reauth:
        credentials = load_credentials()
        if credentials:
            return credentials

    # Need to authenticate
    client_secrets_path = get_client_secrets_path()

    if not client_secrets_path.exists():
        raise FileNotFoundError(
            f"Client secrets file not found at {client_secrets_path}. "
            "Please download it from Google Cloud Console and save it there."
        )

    logger.info("Starting OAuth flow", headless=headless)

    # Allow HTTP for localhost (required for headless OAuth flow)
    import os
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    if headless:
        # Use console-based flow for headless servers
        # Set redirect_uri to localhost for manual copy-paste
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path),
            scopes=SCOPES,
            redirect_uri="http://localhost:8080/",
        )

        # Generate the authorization URL
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

        print("\n" + "=" * 60)
        print("GOOGLE AUTHENTICATION")
        print("=" * 60)
        print("\n1. Open this URL in any browser:\n")
        print(f"   {auth_url}\n")
        print("2. Sign in and authorize the application")
        print("3. You'll be redirected to a page that won't load (localhost)")
        print("4. Copy the FULL URL from your browser's address bar")
        print("\n" + "-" * 60)

        redirect_response = input("\nPaste the full redirect URL here: ").strip()

        # Exchange the authorization code for credentials
        flow.fetch_token(authorization_response=redirect_response)
        credentials = flow.credentials
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path),
            scopes=SCOPES,
        )
        # Run the local server flow for authentication (requires browser)
        credentials = flow.run_local_server(
            port=8080,
            prompt="consent",
            access_type="offline",
        )

    # Save for future use
    save_credentials(credentials)

    logger.info("OAuth flow completed successfully")
    return credentials


def check_credentials_status() -> dict:
    """Check the current status of stored credentials."""
    creds_path = get_credentials_path()
    client_secrets_path = get_client_secrets_path()

    status = {
        "client_secrets_exists": client_secrets_path.exists(),
        "credentials_exists": creds_path.exists(),
        "credentials_valid": False,
        "credentials_expired": False,
        "has_refresh_token": False,
        "scopes": [],
    }

    if creds_path.exists():
        try:
            with open(creds_path) as f:
                creds_data = json.load(f)

            credentials = Credentials(
                token=creds_data.get("token"),
                refresh_token=creds_data.get("refresh_token"),
                token_uri=creds_data.get("token_uri"),
                client_id=creds_data.get("client_id"),
                client_secret=creds_data.get("client_secret"),
                scopes=creds_data.get("scopes"),
            )

            status["credentials_valid"] = credentials.valid
            status["credentials_expired"] = credentials.expired
            status["has_refresh_token"] = bool(credentials.refresh_token)
            status["scopes"] = list(credentials.scopes) if credentials.scopes else []

        except Exception as e:
            status["error"] = str(e)

    return status
