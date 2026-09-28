import ssl

import certifi


def client_ssl_context() -> ssl.SSLContext:
    """Trusts the OS store plus certifi: either one alone can lag behind or miss a CA.

    Always returns a new context: clients like httpx mutate it (ALPN), which breaks other protocols.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(certifi.where())
    return context
