# The scorer builds this on linux/amd64 and runs it with no network.
#
# The selector reads the maps committed under maps/ (built from the pinned sqlglot commit by
# tools/). For commits where it is cheaper, it also runs the candidate tests on the patched
# tree (blast/verify.py), so the image holds sqlglot's source and tests at the pinned commit
# and the packages the organizers labeled with (requirements.lock).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check -r /app/requirements.lock

# sqlglot's own repository at the pinned commit (allowed by the rules)
RUN python -c "import io, tarfile, urllib.request; \
url = 'https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz'; \
data = urllib.request.urlopen(url, timeout=120).read(); \
tf = tarfile.open(fileobj=io.BytesIO(data), mode='r:gz'); \
tf.extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
 && test -f /opt/sqlglot/sqlglot/__init__.py \
 && (python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests || true)

COPY selector.py /app/selector.py
COPY blast /app/blast
COPY maps /app/maps
RUN python -m compileall -q /app/selector.py /app/blast && chmod -R a+rX /app /opt/sqlglot

ENV SKIP_INTEGRATION=0 OMP_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-I", "/app/selector.py"]
