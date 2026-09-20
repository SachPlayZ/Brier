"""Template context available on every page."""
from expenses.views import is_finance


def roles(request):
    """`is_finance` drives the sidebar and the landing page's signed-in button. Views that
    pass it explicitly still win; this only fills it in for the ones that do not."""
    return {"is_finance": is_finance(request.user)}
