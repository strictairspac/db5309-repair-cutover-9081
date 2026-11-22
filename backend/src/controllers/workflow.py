from starlette.responses import Response

from src.services.workflow import assert_current
from src.utils.encoding import canonical


def download(identity):
    # Refuse the export once the evidence is stale: an acceptance sheet must never mix
    # conclusions that were invalidated by a later source update.
    data = assert_current(identity)
    return Response(canonical(data), media_type='application/json', headers={
        'Content-Disposition': f'attachment; filename="evidence-{identity}.json"'})
