"""CSRF protection for state-changing HTTP requests.

CSRF Protection Strategy:
Because session authentication uses HttpOnly cookies, state-changing requests (POST, PUT,
DELETE, PATCH) require CSRF mitigation. We employ a custom header check combined with
double-submit cookie validation:
1. Custom Header Check: Every state-changing request must supply the 'X-Requested-With' header
   (e.g., 'XMLHttpRequest' or 'OpsPilotApp') OR an 'X-CSRF-Token' header matching the 'csrftoken'
   cookie.
2. Cross-origin browser form submissions cannot set arbitrary custom headers (preflight OPTIONS
   is triggered and blocked).
3. The GET /auth/me or login response issues a random CSRF token via cookie/header for single-page apps.

Safe HTTP methods (GET, HEAD, OPTIONS) do not alter server state and are exempt.
Service token authentication (Bearer token from worker/broker) does not use cookies and is exempt.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status

CSRF_HEADER_NAME = "x-csrf-token"
CSRF_COOKIE_NAME = "csrftoken"
REQUESTED_WITH_HEADER = "x-requested-with"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def generate_csrf_token() -> str:
    """Generates a cryptographically random CSRF token."""
    return secrets.token_urlsafe(32)


def verify_csrf(request: Request, is_service_request: bool = False) -> None:
    """Validates CSRF tokens or custom headers on mutating requests.

    Exemptions:
    - Safe methods (GET, HEAD, OPTIONS)
    - Service principal requests using Authorization: Bearer <service_token>
    """
    if request.method in SAFE_METHODS or is_service_request:
        return

    # Check for custom header or matching double-submit cookie
    custom_header = request.headers.get(REQUESTED_WITH_HEADER)
    csrf_header = request.headers.get(CSRF_HEADER_NAME)
    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)

    # 1. Custom header check (browser CORS blocks custom headers from cross-origin requests)
    if custom_header:
        return

    # 2. Double-submit cookie verification
    if csrf_header and csrf_cookie and hmac.compare_digest(csrf_header, csrf_cookie):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="CSRF verification failed: missing custom header (X-Requested-With) or mismatched X-CSRF-Token",
    )
