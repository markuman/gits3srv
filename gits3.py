#!/usr/bin/env python3
"""gits3.py – git smart-http vorne, ein s3-bucket als source of truth hinten.

layout pro repo im bucket (= gültiges bare repo, bucket runterladen -> `git clone` geht direkt):
    <prefix>foo.git/HEAD, config, packed-refs, refs/.keep, objects/info/.keep,
    <prefix>foo.git/objects/pack/pack-*.pack + pack-*.idx
regeln:
    - nur packs (receive.unpackLimit=1), nie loose objects
    - nur packed-refs, nie loose refs. packed-refs ist das manifest -> cas per If-Match
    - schreibreihenfolge: pack, idx, packed-refs. ein repo entsteht erst mit dem ersten angenommenen push
    - .keep-dateien, weil s3 keine leeren ordner kennt und git ohne refs/ + objects/ kein repo sieht
lokal liegt nur ein wegwerf-cache (GITS3_CACHE), bedient von `git http-backend`.

zugriff:
    - schreiben (push) braucht immer ein login mit rolle rw
    - lesen (clone/fetch) geht ohne login, wenn im repo die datei `git-daemon-export-ok` liegt
      (git-konvention; in der nextcloud anlegen/löschen = repo öffentlich/privat schalten),
      sonst braucht es ein login (ro oder rw). neue repos sind privat.
    - die index-seite zeigt anonym nur öffentliche repos, /login fragt nach dem login

der s3-server muss conditional writes können (PUT mit If-Match / If-None-Match: *).
aws s3 kann das seit 2024, minio, garage & co. je nach version.

env:
    GITS3_ENDPOINT        z.b. https://s3.eu-central-1.amazonaws.com (path-style)
    GITS3_BUCKET          bucket-name
    GITS3_PREFIX          optional, z.b. "git/"
    GITS3_REGION          default us-east-1
    AWS_ACCESS_KEY_ID     s3-key, idealerweise nur für diesen bucket
    AWS_SECRET_ACCESS_KEY
    GITS3_USERS           logins, leerzeichen-getrennt: "m:pw:rw agent:pw2:ro"
                          rw = lesen + schreiben, ro = nur lesen (alle repos, auch private)
                          (altes GITS3_AUTH=user:pass geht weiter und zählt als rw)
    GITS3_CACHE           lokaler cache (default ./cache)
    GITS3_LISTEN          default 127.0.0.1:8000
    GITS3_MAX_BODY        max. größe eines push-bodys in bytes, entpackt (default 2 GiB).
                          clone/fetch-bodys (wants/haves, auch anonym) sind fest auf 64 MiB begrenzt
    GITS3_INDEX_TTL       sekunden, die die repo-liste der index-seite gecacht wird (default 30)
"""
import base64, fcntl, hashlib, hmac, json, os, re, shutil, signal, socket, subprocess, sys, tempfile, threading, time, zlib
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_users(spec, var="GITS3_USERS"):
    """'m:pw:rw agent:pw2:ro' -> {user: (pw, role)}. das passwort darf ':' enthalten, aber keine leerzeichen."""
    users = {}
    for entry in spec.split():
        user, _, rest = entry.partition(":")
        pw, _, role = rest.rpartition(":")
        if not user or not pw or role not in ("ro", "rw"):
            sys.exit(f"gits3: ungültiger eintrag in {var} für {user!r}, erwartet user:passwort:ro|rw")
        users[user] = (pw, role)
    return users


USERS = parse_users(os.environ.get("GITS3_USERS", ""))
if os.environ.get("GITS3_AUTH"):  # veraltet: user:passwort, zählt als rw. gleiche prüfung, kein leeres passwort
    for _u, _v in parse_users(os.environ["GITS3_AUTH"] + ":rw", "GITS3_AUTH").items():
        USERS.setdefault(_u, _v)
EXPORT_MARKER = "git-daemon-export-ok"
CACHE = os.path.abspath(os.environ.get("GITS3_CACHE", "cache"))
LISTEN = os.environ.get("GITS3_LISTEN", "127.0.0.1:8000")
MAX_BODY = int(os.environ.get("GITS3_MAX_BODY", 2 << 30))
FETCH_BODY = 64 << 20  # upload-pack schickt nur wants/haves. anonym erreichbar, daher klein
INDEX_TTL = float(os.environ.get("GITS3_INDEX_TTL", 30))

REPO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.git")
REPO_RE = re.compile(rf"/({REPO_NAME_RE.pattern})(/info/refs|/git-upload-pack|/git-receive-pack)")
GC_RE = re.compile(rf"/({REPO_NAME_RE.pattern})/gc")
PACKED_REFS_HEADER = b"# pack-refs with: peeled fully-peeled sorted \n"
LOCAL_CONFIG = {
    "receive.unpackLimit": "1",      # jeder push -> pack, keine loose objects
    "gc.auto": "0",                  # cache nie selbst repacken
    "http.receivepack": "true",
    "core.logAllRefUpdates": "false",
    "pack.writeReverseIndex": "false",
    "receive.fsckObjects": "true",   # kaputtes oder bösartiges bleibt draußen, der bucket ist append-only
    "receive.autogc": "false",       # kein `git maintenance run --auto` nach jedem push
    "maintenance.auto": "false",
}
# git sieht nur, was wir ihm geben: keine user-/system-config (hooks, templateDir, reftable, …)
# und keine GIT_*-variablen oder secrets aus unserer umgebung. ältere gits ignorieren die neuen variablen
GIT_ENV = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "TZ") or k.startswith("LC_")}
GIT_ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_DEFAULT_REF_FORMAT="files",
               # der cache gehört uns, auch wenn er (bind-mount) einer anderen uid gehört. safe.directory
               # zählt nur aus geschützter config, GIT_CONFIG_* ist command-scope und damit geschützt
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0="*")
EMPTY = ".empty.git"  # leeres repo für info/refs von receive-pack auf neue repos, kein gültiger repo-name


class S3Error(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class S3:
    """minimaler s3-client: sigv4, path-style, nur get/put/list. nur stdlib."""

    def __init__(self, endpoint, bucket, prefix, region, key, secret):
        u = urllib.parse.urlsplit(endpoint.rstrip("/"))
        self.scheme, self.host, self.base = u.scheme, u.netloc, u.path
        self.bucket, self.prefix, self.region = bucket, prefix, region
        self.key, self.secret = key, secret

    @staticmethod
    def _q(s, safe="-_.~"):
        return urllib.parse.quote(s, safe=safe)

    @staticmethod
    def _sha256(data):
        if data is None:
            data = b""
        if isinstance(data, bytes):
            return hashlib.sha256(data).hexdigest()
        h = hashlib.sha256()
        for chunk in iter(lambda: data.read(1 << 20), b""):
            h.update(chunk)
        data.seek(0)
        return h.hexdigest()

    def req(self, method, key=None, data=None, headers=None, query=None, stream=False):
        """ein request, bei 5xx und netzfehlern bis zu zweimal wiederholt.
        stream=True gibt die offene response zurück, sonst (status, headers, body)."""
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            if hasattr(data, "seek"):
                data.seek(0)
            try:
                resp = self._send(method, key, data, headers, query)
                if resp.getcode() >= 500 and attempt < 2:
                    print(f"gits3: s3 {method} {key} -> {resp.getcode()}, nochmal", file=sys.stderr)
                    resp.close()
                    continue
                if stream:
                    return resp
                with resp:
                    return resp.getcode(), resp.headers, resp.read()
            except OSError as e:  # timeout, reset, dns, …
                if attempt == 2:
                    raise
                print(f"gits3: s3 {method} {key} -> {e!r}, nochmal", file=sys.stderr)

    def _send(self, method, key, data, headers, query):
        path = f"{self.base}/{self.bucket}" + ("" if key is None else f"/{self.prefix}{key}")
        uri = self._q(path, safe="/-_.~")
        cq = "&".join(f"{self._q(k)}={self._q(v)}" for k, v in sorted((query or {}).items()))

        amz = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        scope = f"{amz[:8]}/{self.region}/s3/aws4_request"
        payload = self._sha256(data)
        sig_h = {"host": self.host, "x-amz-content-sha256": payload, "x-amz-date": amz}
        signed = ";".join(sorted(sig_h))
        creq = "\n".join([method, uri, cq, "".join(f"{k}:{sig_h[k]}\n" for k in sorted(sig_h)), signed, payload])
        sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
        k = ("AWS4" + self.secret).encode()
        for part in (amz[:8], self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()

        h = {"Host": self.host, "x-amz-content-sha256": payload, "x-amz-date": amz,
             "Authorization": f"AWS4-HMAC-SHA256 Credential={self.key}/{scope}, "
                              f"SignedHeaders={signed}, Signature={sig}", **(headers or {})}
        if method == "PUT":  # sonst setzt urllib form-encoding und manche server fressen den body
            h.setdefault("Content-Type", "application/octet-stream")
            h["Content-Length"] = str(os.fstat(data.fileno()).st_size if hasattr(data, "read") else len(data or b""))
        url = f"{self.scheme}://{self.host}{uri}" + (f"?{cq}" if cq else "")
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            return urllib.request.urlopen(r, timeout=600)
        except urllib.error.HTTPError as e:
            return e

    def get(self, key):
        return self.req("GET", key)

    def put(self, key, data, headers=None):
        st, _, body = self.req("PUT", key, data, headers)
        if st != 200:
            raise S3Error(f"PUT {key} -> {st} {body[:200]!r}", st)

    def objects(self, prefix):
        """(key relativ zu GITS3_PREFIX, LastModified) aller objekte unter prefix, mit paginierung."""
        out_list, token = [], None
        while True:
            q = {"list-type": "2", "prefix": self.prefix + prefix}
            if token:
                q["continuation-token"] = token
            st, _, out = self.req("GET", query=q)
            if st != 200:
                raise S3Error(f"LIST {prefix} -> {st} {out[:200]!r}")
            root = ET.fromstring(out)
            for c in root.iterfind(".//{*}Contents"):
                key = c.findtext("{*}Key") or ""
                out_list.append((key[len(self.prefix):], c.findtext("{*}LastModified") or ""))
            if (root.findtext("{*}IsTruncated") or "false").lower() != "true":
                return out_list
            token = root.findtext("{*}NextContinuationToken")

    def ls(self, prefix):
        """namen der keys direkt unter prefix. tiefere keys (prefix/sub/x) zählen nicht."""
        return [k[len(prefix):] for k, _ in self.objects(prefix) if "/" not in k[len(prefix):]]

    def download(self, key, dest):
        resp = self.req("GET", key, stream=True)
        with resp:
            if resp.getcode() != 200:
                raise S3Error(f"GET {key} -> {resp.getcode()}")
            with open(dest + ".tmp", "wb") as f:
                shutil.copyfileobj(resp, f, 1 << 20)
        os.replace(dest + ".tmp", dest)


_prefix = os.environ.get("GITS3_PREFIX", "").strip("/")  # "repos", "/repos/" -> "repos/"
s3 = S3(os.environ["GITS3_ENDPOINT"], os.environ["GITS3_BUCKET"], _prefix + "/" if _prefix else "",
        os.environ.get("GITS3_REGION", "us-east-1"),
        os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"])


# ---------------------------------------------------------------- cache <-> s3

def local_dir(repo):
    return os.path.join(CACHE, repo)


def git(repo_dir, *args):
    subprocess.run(["git", "-C", repo_dir, *args], check=True, stdout=subprocess.DEVNULL, env=GIT_ENV)


def read(path, default=b""):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return default


def write_atomic(path, data):
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


@contextmanager
def synced(repo, push):
    """cache auf remote-stand bringen und festhalten, solange der block läuft. liefert den etag
    von packed-refs, "" für ein repo, das es (noch) nur lokal gibt, None, wenn es fehlt.

    zwei flocks pro repo, weil ein flock-downgrade nicht atomar ist:
      <repo>.lock  serialisiert sync und push
      <repo>.use   schützt den cache, solange git ihn liest. push hält es exklusiv,
                   clone/fetch geteilt bis zum ende des streams
    lesende requests geben .lock nach dem sync frei, parallele clones laufen also parallel.
    das geht, weil sync_down neben laufenden clones nur hinzufügt oder atomar ersetzt (packs vor
    refs). gelöscht wird nur, was nicht im bucket liegt, und das gibt es nur nach einem absturz.
    dann läuft der erste, volle sync im neuen prozess, bevor irgendwer streamt."""
    os.makedirs(CACHE, exist_ok=True)
    base = os.path.join(CACHE, repo)
    with open(base + ".lock", "a") as sync, open(base + ".use", "a") as use:
        fcntl.flock(sync, fcntl.LOCK_EX)
        fcntl.flock(use, fcntl.LOCK_EX if push else fcntl.LOCK_SH)
        etag = sync_down(repo, create=push)
        if not push:
            fcntl.flock(sync, fcntl.LOCK_UN)
        try:
            yield etag
        except BaseException:
            if push:  # push mittendrin abgebrochen, evtl. auch rollback(): nächster sync gleicht voll ab
                _checked.discard(repo)
            raise


_checked = set()  # repos, deren cache in diesem prozess schon einmal voll abgeglichen wurde


def configure(local):
    """LOCAL_CONFIG setzen. bei jedem vollen sync, damit auch alte caches änderungen mitkriegen."""
    for k, v in LOCAL_CONFIG.items():
        git(local, "config", k, v)


def init_local(repo):
    local = local_dir(repo)
    shutil.rmtree(local, ignore_errors=True)
    subprocess.run(["git", "init", "-q", "--bare", "--template=", local],  # ohne template: keine hooks
                   check=True, stdout=subprocess.DEVNULL, env=GIT_ENV)
    write_atomic(os.path.join(local, "HEAD"), b"ref: refs/heads/main\n")
    configure(local)


def empty_repo():
    """leeres repo für info/refs von receive-pack auf ein neues repo. dessen ref-liste ist für alle
    leeren repos gleich, angelegt wird erst beim POST. so entsteht kein cache-dir pro tippfehler."""
    with _empty_lock:
        if not os.path.isfile(os.path.join(local_dir(EMPTY), "HEAD")):
            init_local(EMPTY)


_empty_lock = threading.Lock()


def clear_locks(local):
    """*.lock eines abgeschossenen git (packed-refs.lock, config.lock, HEAD.lock) blockieren sonst
    jeden weiteren push. nur, während .lock exklusiv gehalten wird, dann schreibt kein git."""
    for n in os.listdir(local):
        if n.endswith(".lock"):
            os.remove(os.path.join(local, n))


def set_refs(local, refs):
    """packed-refs ist die einzige ref-quelle: atomar ersetzen, loose refs weg.
    löscht nur dateien, nie refs/ selbst, sonst sieht ein parallel startendes git kein repo."""
    write_atomic(os.path.join(local, "packed-refs"), refs)
    for d, _, files in os.walk(os.path.join(local, "refs")):
        for f in files:
            os.remove(os.path.join(d, f))
    os.makedirs(os.path.join(local, "refs", "heads"), exist_ok=True)
    os.makedirs(os.path.join(local, "refs", "tags"), exist_ok=True)


def sync_down(repo, create=False):
    """cache auf remote-stand bringen. rückgabe siehe synced()."""
    local = local_dir(repo)
    # packed-refs ist winzig: immer komplett holen und inhalt vergleichen, statt
    # auf If-None-Match zu vertrauen (manche backends haben nur sekundengenaue etags)
    st, h, refs = s3.get(f"{repo}/packed-refs")
    if st == 404:
        if not create:
            return None
        # neues repo nur lokal anlegen. in den bucket kommt es erst mit dem ersten
        # angenommenen push (push_up), ein --dry-run oder abgelehnter push hinterlässt nichts
        init_local(repo)
        set_refs(local, PACKED_REFS_HEADER)
        return ""
    if st != 200:
        raise S3Error(f"GET {repo}/packed-refs -> {st}", st)
    etag = h.get("ETag", "")
    if not etag:
        raise S3Error("s3-backend liefert keinen ETag für packed-refs, ohne geht kein cas (If-Match)")

    if not os.path.isdir(local):
        init_local(repo)
    # schnellweg nur, wenn der cache in diesem prozess schon mal voll abgeglichen wurde. nach einem
    # absturz (kill -9, oom, strom) zwischen receive-pack und push_up lief rollback() nicht: dann
    # liegen loose refs und packs im cache, die der bucket nicht kennt, und packed-refs ist trotzdem gleich.
    # HEAD vergleicht der schnellweg nicht (kostet einen GET pro request): ein HEAD, der sich im bucket
    # ohne packed-refs ändert (fix_head einer anderen instanz, handarbeit), kommt erst mit der nächsten
    # änderung an packed-refs an
    if repo in _checked and refs == read(os.path.join(local, "packed-refs"), None):
        return etag
    _checked.discard(repo)  # bricht der volle sync ab, beim nächsten mal wieder voll
    clear_locks(local)
    configure(local)

    # packs holen, die wir noch nicht haben. nur komplette paare (pack + idx)
    pdir = os.path.join(local, "objects", "pack")
    names = set(s3.ls(f"{repo}/objects/pack/"))
    keep = {n[:-5] + ext for n in names if n.endswith(".pack") and n[:-5] + ".idx" in names
            for ext in (".pack", ".idx")}
    for n in sorted(keep):
        if not os.path.exists(os.path.join(pdir, n)):
            s3.download(f"{repo}/objects/pack/{n}", os.path.join(pdir, n))

    st, _, head = s3.get(f"{repo}/HEAD")
    if st == 200:
        write_atomic(os.path.join(local, "HEAD"), head)
    set_refs(local, refs)  # remote gewinnt, refs zuletzt: die objekte sind dann schon da
    # regel: im cache liegen nur packs, die auch im bucket liegen. alles andere (reste eines
    # abgestürzten pushs, abgebrochene downloads, tmp_pack_*) weg. sicher: keine ref aus dem
    # bucket zeigt darauf, und ein push läuft nicht parallel (.lock)
    for n in set(os.listdir(pdir)) - keep:
        os.remove(os.path.join(pdir, n))
    _checked.add(repo)
    return etag


def create_remote(repo):
    """gerüst eines neuen repos. nur anlegen, was fehlt: 412 heißt schon da, alles andere ist ein fehler."""
    for name, data in (("HEAD", b"ref: refs/heads/main\n"),
                       ("config", b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n"),
                       ("refs/.keep", b""), ("objects/info/.keep", b"")):
        try:
            s3.put(f"{repo}/{name}", data, {"If-None-Match": "*"})
        except S3Error as e:
            if e.status != 412:
                raise


def ref_lines(refs):
    """packed-refs ohne kommentare. der header (# pack-refs with: …) hängt an der git-version."""
    return [l for l in refs.splitlines() if not l.startswith(b"#")]


def push_up(repo, etag, before_refs, before_packs):
    """nach receive-pack: neue packs hoch, dann packed-refs per cas. False, wenn git keine ref
    geändert hat (abgelehnt, no-op), dann geht nichts hoch. wirft S3Error, bei konflikt mit 412."""
    local = local_dir(repo)
    pdir = os.path.join(local, "objects", "pack")
    git(local, "pack-refs", "--all", "--prune")
    refs = read(os.path.join(local, "packed-refs"))
    if ref_lines(refs) == ref_lines(before_refs):
        return False

    for n in sorted(set(os.listdir(pdir)) - before_packs):
        if n.endswith(".pack"):
            for ext in (".pack", ".idx"):  # idx nach pack: ls() nimmt nur komplette paare
                with open(os.path.join(pdir, n[:-5] + ext), "rb") as f:
                    s3.put(f"{repo}/objects/pack/{n[:-5]}{ext}", f)

    if etag:
        cond = {"If-Match": etag}
    else:  # neues repo: erst das gerüst, packed-refs zuletzt. erst damit existiert es
        create_remote(repo)
        cond = {"If-None-Match": "*"}
    s3.put(f"{repo}/packed-refs", refs, cond)
    return True


def rollback(repo, before_refs, before_packs):
    """cache auf den stand vor dem push. neue packs müssen weg: schickt ein späterer push dasselbe
    pack (gleicher name), stünde es in before_packs und ginge nie in den bucket."""
    local = local_dir(repo)
    pdir = os.path.join(local, "objects", "pack")
    for n in set(os.listdir(pdir)) - before_packs:
        os.remove(os.path.join(pdir, n))
    clear_locks(local)
    set_refs(local, before_refs)


def gc(repo, etag):
    """repack -a -d im cache, alle neuen packs hoch, alte packs aus dem bucket weg. nur unter synced(push=True).
    unerreichbare objekte (z.b. gelöschte entwicklungsbranches) fliegen dabei raus.

    reihenfolge: packs hoch, dann packed-refs per cas mit IDENTISCHEM inhalt (ändert nur den etag), erst
    danach ls + delete. ein paralleler push einer zweiten instanz hat den alten etag: sein cas bekommt 412,
    er rollt zurück, sein pack bleibt als waise im bucket bis zum nächsten gc. bei 412 bleiben alte packs liegen.
    sekundengenaue etags (nextcloud) sind dieselbe grenze wie bei pushes."""
    local = local_dir(repo)
    pdir = os.path.join(local, "objects", "pack")
    prefix = f"{repo}/objects/pack/"
    _checked.discard(repo)  # repack räumt den cache um, der nächste request gleicht voll ab
    cache_before = {n[:-5] for n in os.listdir(pdir) if n.endswith(".pack")}
    size_before = sum(os.path.getsize(os.path.join(pdir, n + ".pack")) for n in cache_before)
    # bare repo schreibt sonst .bitmap, das gehört nicht zum layout im bucket
    git(local, "-c", "repack.writeBitmaps=false", "-c", "pack.writeBitmaps=false", "repack", "-a", "-d", "-q")
    packs = sorted(n[:-5] for n in os.listdir(pdir) if n.endswith(".pack"))
    if not packs:  # nichts erreichbar: ohne pack kein upload, alte packs bleiben im bucket
        return f"{repo}: nichts erreichbar, nichts zu tun"
    bucket_before = s3.ls(prefix)  # zählt, was wirklich im bucket liegt (inkl. waisen eines abgebrochenen gc)
    stems_before = {n[:-5] for n in bucket_before if n.endswith(".pack")}
    if len(packs) == 1 and packs == sorted(cache_before) and set(bucket_before) == {packs[0] + ".pack", packs[0] + ".idx"}:
        return f"{repo}: nichts zu tun"  # genau ein pack, derselbe name vor und nach dem repack: kein re-upload
    size_after = sum(os.path.getsize(os.path.join(pdir, n + ".pack")) for n in packs)
    # komplette paare, die nach einem 412 schon oben liegen: der name ist deterministisch, also nicht nochmal hoch
    pairs_before = {n[:-4] for n in bucket_before if n.endswith(".idx") and n[:-4] + ".pack" in bucket_before}
    for stem in packs:  # alle packs, nicht nur das erste: repack kann mehrere übrig lassen
        if stem in pairs_before:
            continue
        for ext in (".pack", ".idx"):  # idx nach pack, ls() nimmt nur komplette paare
            with open(os.path.join(pdir, stem + ext), "rb") as f:
                s3.put(f"{repo}/objects/pack/{stem}{ext}", f)
    # cas mit identischem inhalt: der etag ändert sich, ein paralleler push mit altem etag bekommt 412.
    # das passiert vor ls/delete, nicht als GET-vergleich davor (der wäre toctou)
    refs = read(os.path.join(local, "packed-refs"))
    try:
        s3.put(f"{repo}/packed-refs", refs, {"If-Match": etag})
    except S3Error as e:
        if e.status == 412:
            return f"{repo}: packed-refs hat sich während des gc geändert, alte packs bleiben liegen (nochmal gc)"
        raise
    old = [n for n in s3.ls(prefix) if not any(n.startswith(s + ".") for s in packs)]
    old.sort(key=lambda n: not n.endswith(".idx"))  # idx zuerst: ohne idx ist ein pack unsichtbar
    for n in old:
        st, _, body = s3.req("DELETE", prefix + n)
        if st not in (200, 204):
            raise S3Error(f"DELETE {n} -> {st} {body[:200]!r}", st)
    return (f"{repo}: {len(stems_before)} packs → {len(packs)}, "
            f"{size_before / 2**20:.1f} MB → {size_after / 2**20:.1f} MB (größe aus dem cache)")


def fix_head(repo):
    """zeigt HEAD auf einen branch, den es nicht gibt (z.b. main, gepusht wurde nur master),
    auf einen existierenden umbiegen. sonst checkt `git clone` nichts aus."""
    local = local_dir(repo)
    head = read(os.path.join(local, "HEAD")).decode().strip()
    refs = read(os.path.join(local, "packed-refs"))
    branches = [l.split()[1] for l in refs.decode().splitlines()
                if not l.startswith(("#", "^")) and l.split()[1:2] and l.split()[1].startswith("refs/heads/")]
    if not branches or head.removeprefix("ref: ") in branches:
        return
    target = next((b for b in ("refs/heads/main", "refs/heads/master") if b in branches), sorted(branches)[0])
    new_head = f"ref: {target}\n".encode()
    s3.put(f"{repo}/HEAD", new_head)
    write_atomic(os.path.join(local, "HEAD"), new_head)


# ---------------------------------------------------------------- index-seite

def is_public(repo):
    """öffentlich = marker-datei liegt im repo. im zweifel (fehler, kein HEAD-support) privat."""
    return s3.req("HEAD", f"{repo}/{EXPORT_MARKER}")[0] == 200


_index = None  # (zeitpunkt, repos) – das listing geht über alle packs, daher kurz cachen


def list_repos():
    """[{name, mtime, public}] aller repos, neueste zuerst. letzte änderung = LastModified von packed-refs.
    ein einziges listing liefert beides: packed-refs und die marker-dateien."""
    global _index
    if _index and time.monotonic() - _index[0] < INDEX_TTL:
        return _index[1]
    objs = [(k.split("/"), lm) for k, lm in s3.objects("") if k.count("/") == 1]
    public = {parts[0] for parts, _ in objs if parts[1] == EXPORT_MARKER}
    repos = [{"name": parts[0], "mtime": lm, "public": parts[0] in public}
             for parts, lm in objs if parts[1] == "packed-refs" and REPO_NAME_RE.fullmatch(parts[0])]
    repos.sort(key=lambda r: r["mtime"], reverse=True)
    _index = (time.monotonic(), repos)
    return repos


def forget_index():
    global _index
    _index = None


INDEX_HTML = """<!doctype html>
<html lang="de">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>git</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--mute:#78716c;--line:#e7e5e4;--hover:#f2f1ef;--acc:#0f766e;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#0f0e0d;--fg:#e7e5e4;--mute:#8f8984;--line:#262321;--hover:#1a1817;--acc:#2dd4bf}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:40rem;margin:0 auto;padding:clamp(2rem,8vh,5rem) 1rem}
header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:1.25rem}
h1{margin:0;font-size:.78rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--mute)}
small{color:var(--mute);font-size:.8rem}
small a{color:inherit;text-underline-offset:3px}
small a:hover{color:var(--fg)}
footer{margin-top:1rem;text-align:right}
.badge{margin-left:.6rem;padding:.12rem .4rem;border:1px solid var(--line);border-radius:4px;color:var(--mute);
  font:600 .62rem/1 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;letter-spacing:.1em;text-transform:uppercase;vertical-align:.12rem}
input{width:100%;margin-bottom:1rem;padding:.6rem .75rem;font:inherit;color:inherit;background:transparent;
  border:1px solid var(--line);border-radius:6px;outline:none;transition:border-color .12s}
input:focus{border-color:var(--acc)}
input::placeholder{color:var(--mute)}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;padding:.85rem .6rem}
.head{padding-top:0;padding-bottom:.4rem;border-bottom:1px solid var(--line)}
.head button{all:unset;cursor:pointer;color:var(--mute);font-size:.72rem;font-weight:600;letter-spacing:.12em;
  text-transform:uppercase;border-radius:4px}
.head button:hover,.head button[aria-sort]{color:var(--fg)}
.head button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.head button[aria-sort=ascending]::after{content:" ↑"}
.head button[aria-sort=descending]::after{content:" ↓"}
ul{list-style:none;margin:0;padding:0}
li{border-bottom:1px solid var(--line)}
li>button{all:unset;box-sizing:border-box;width:100%;cursor:pointer;border-radius:6px;transition:background .12s}
li>button:hover{background:var(--hover)}
li>button:focus-visible{outline:2px solid var(--acc);outline-offset:-2px}
.n{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.93rem;overflow:hidden;text-overflow:ellipsis}
.n span{color:var(--mute)}
.t{color:var(--mute);font-size:.84rem;white-space:nowrap}
.empty{padding:1rem .6rem;color:var(--mute)}
#toast{position:fixed;left:50%;bottom:2rem;max-width:calc(100% - 2rem);transform:translate(-50%,.75rem);opacity:0;
  background:var(--fg);color:var(--bg);padding:.5rem 1rem;border-radius:999px;font-size:.84rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:opacity .18s,transform .18s;pointer-events:none}
#toast.on{opacity:1;transform:translate(-50%,0)}
</style>
<main>
<header><h1>repos</h1><small id="who"></small></header>
<input id="q" type="search" placeholder="suchen …" autocomplete="off" spellcheck="false" autofocus>
<div class="row head"><button data-k="name">name</button><button data-k="mtime">geändert</button></div>
<ul id="l"></ul>
<footer><small>klick kopiert den clone-befehl</small></footer>
</main>
<div id="toast" role="status"></div>
<script>
const {user, repos} = __DATA__;
const who = document.getElementById("who");
if (user) who.textContent = "angemeldet als " + user;
else who.innerHTML = 'öffentliche repos · <a href="/login">anmelden</a>';
const rtf = new Intl.RelativeTimeFormat(navigator.language, {numeric: "auto"});
const units = [["year", 31536e3], ["month", 2592e3], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60]];
function ago(date) {
  const s = (date - Date.now()) / 1e3;
  for (const [u, n] of units) if (Math.abs(s) >= n) return rtf.format(Math.round(s / n), u);
  return rtf.format(0, "second");
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {  // http ohne localhost: kein secure context -> alter weg
    const a = document.createElement("textarea");
    a.value = text; document.body.append(a); a.select(); document.execCommand("copy"); a.remove();
  }
}
let timer;
function toast(text) {
  const t = document.getElementById("toast");
  t.textContent = text; t.classList.add("on");
  clearTimeout(timer); timer = setTimeout(() => t.classList.remove("on"), 1800);
}

const list = document.getElementById("l"), q = document.getElementById("q");
const heads = document.querySelectorAll(".head button");
let sort = {k: "mtime", dir: -1};  // start: neueste zuerst

function item(r) {
  const cmd = `git clone ${location.origin}/${r.name}`;
  const li = document.createElement("li"), b = document.createElement("button");
  const n = document.createElement("span"), ext = document.createElement("span"), t = document.createElement("span");
  b.className = "row"; n.className = "n"; t.className = "t";
  n.textContent = r.name.replace(/\\.git$/, ""); ext.textContent = ".git"; n.append(ext);
  if (user && !r.public) { const p = document.createElement("span"); p.className = "badge"; p.textContent = "privat"; n.append(p); }
  const d = new Date(r.mtime);
  t.textContent = isNaN(d) ? "" : ago(d); t.title = isNaN(d) ? "" : d.toLocaleString();
  b.title = cmd; b.append(n, t);
  b.onclick = () => copy(cmd).then(() => toast("kopiert: " + cmd));
  li.append(b);
  return li;
}

function render() {
  const needle = q.value.trim().toLowerCase();
  const rows = repos.filter(r => r.name.toLowerCase().includes(needle))
    .sort((a, b) => sort.dir * (sort.k === "name" ? a.name.localeCompare(b.name) : a.mtime.localeCompare(b.mtime)));
  heads.forEach(h => h.dataset.k === sort.k
    ? h.setAttribute("aria-sort", sort.dir > 0 ? "ascending" : "descending") : h.removeAttribute("aria-sort"));
  list.replaceChildren(...rows.map(item));
  if (!rows.length) list.innerHTML = `<li class="empty">${repos.length ? "keine treffer." : user ? "noch keine repos. einfach eins pushen." : "keine öffentlichen repos."}</li>`;
}

heads.forEach(h => h.onclick = () => {
  const k = h.dataset.k;
  sort = sort.k === k ? {k, dir: -sort.dir} : {k, dir: k === "name" ? 1 : -1};
  render();
});
q.oninput = render;
// einfach lostippen: druckbare tasten landen immer im suchfeld, escape leert es
document.onkeydown = e => {
  if (e.key === "Escape") { q.value = ""; render(); q.blur(); }
  else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey && document.activeElement !== q) q.focus();
};
render();
</script>
</html>
"""


def index_page(user):
    repos = [r for r in list_repos() if user or r["public"]]
    data = json.dumps({"user": user, "repos": repos}).replace("<", "\\u003c")  # kein </script>, kein <!--
    return INDEX_HTML.replace("__DATA__", data).encode()


# ---------------------------------------------------------------- http frontend

class Fail(Exception):
    """request mit fehlercode abbrechen, z.b. 413 beim body."""
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    server_version = "gits3/0.1"
    sys_version = ""  # kein "Python/3.x" im Server-header
    timeout = 300  # socket-timeout pro lese-/schreibvorgang, gegen slowloris

    def do_GET(self):
        self.headers_sent = False
        try:
            self.handle_git()
        except Exception as e:
            self.log_error("fehler: %r", e)
            self.close_connection = True
            if not self.headers_sent:  # mitten im stream keine zweite antwort hinterherschieben
                try:
                    self.text(e.code if isinstance(e, Fail) else 500, f"gits3: {e}")
                except OSError:
                    pass  # client schon weg

    do_POST = do_GET

    def end_headers(self):
        self.headers_sent = True
        super().end_headers()

    def text(self, code, msg, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode() + b"\n")

    def login(self):
        """-> (user, role, bad). ohne header: (None, None, False). falsche daten: bad=True."""
        h = self.headers.get("Authorization", "")
        if not h:
            return None, None, False
        if not h.startswith("Basic "):
            return None, None, True
        try:
            user, _, pw = base64.b64decode(h[6:], validate=True).decode().partition(":")
        except ValueError:  # binascii.Error, UnicodeDecodeError
            return None, None, True
        want = USERS.get(user)
        # auch bei unbekanntem user vergleichen, und über hashes gleicher länge,
        # damit die laufzeit weder user noch passwortlänge verrät
        digest = lambda s: hashlib.sha256(s.encode()).digest()
        ok = hmac.compare_digest(digest(pw), digest(want[0] if want else "\0")) and want is not None
        return (user, want[1], False) if ok else (None, None, True)

    def deny(self):
        return self.text(401, "auth required", [("WWW-Authenticate", 'Basic realm="gits3", charset="UTF-8"')])

    def read_body(self, f, limit):
        """body nach f, max. limit bytes. gzip wird hier entpackt, damit das limit für die entpackte
        größe gilt (sonst: gzip-bombe). -> größe."""
        too_big = Fail(413, f"body größer als {limit} bytes")
        enc = self.headers.get("Content-Encoding", "identity").lower()
        if enc not in ("identity", "gzip", "x-gzip"):
            raise Fail(415, f"Content-Encoding {enc} kann ich nicht")
        gz = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc != "identity" else None
        chunked = self.headers.get("Transfer-Encoding", "").lower() == "chunked"
        cl = self.headers.get("Content-Length") or "0"
        if not chunked and not cl.isdigit():
            raise Fail(400, "kaputtes Content-Length")
        n = 0 if chunked else int(cl)
        if n > limit:
            raise too_big
        # der server spricht HTTP/1.0, der stdlib-automatismus greift daher nicht. ohne antwort
        # wartet curl bei großen bodies erst seinen timeout ab
        if self.request_version == "HTTP/1.1" and self.headers.get("Expect", "").lower() == "100-continue":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")

        total = 0

        def emit(data):
            nonlocal total
            total += len(data)
            if total > limit:
                raise too_big
            f.write(data)

        def copy(n):
            while n > 0:
                raw = self.rfile.read(min(n, 1 << 20))
                if not raw:
                    raise Fail(400, "body kürzer als angekündigt")
                n -= len(raw)
                while raw:  # in häppchen entpacken, nie mehr als 1 MiB auf einmal im ram
                    try:
                        out, raw = (gz.decompress(raw, 1 << 20), gz.unconsumed_tail) if gz else (raw, b"")
                    except zlib.error as e:
                        raise Fail(400, f"kaputtes gzip: {e}") from None
                    emit(out)

        if chunked:
            raw_total = 0
            while True:
                m = re.fullmatch(rb"([0-9a-fA-F]{1,16})(;[^\r\n]*)?\r?\n", self.rfile.readline(1024))
                if not m:
                    raise Fail(400, "kaputtes chunked encoding")
                size = int(m[1], 16)
                if not size:
                    break
                raw_total += size
                if raw_total > limit:
                    raise too_big
                copy(size)
                if self.rfile.readline(1024) not in (b"\r\n", b"\n"):
                    raise Fail(400, "chunk ohne abschließendes crlf")
            while self.rfile.readline(1024).strip():  # trailer bis zur leerzeile
                pass
        else:
            copy(n)
        if gz:
            emit(gz.flush())
        f.flush()
        size = f.tell()
        f.seek(0)
        return size

    @contextmanager
    def cgi(self, url, body, size, user):
        """git http-backend starten. liefert (stdout, status, headers); der prozess wird im
        finally immer eingesammelt, bei abbruch (client weg, fehler) vorher gekillt."""
        # whitelist statt os.environ: keine secrets an git, keine GIT_*-injektion. den body
        # hat read_body schon entpackt, Content-Encoding geht deshalb nicht mit
        env = dict(GIT_ENV, GIT_PROJECT_ROOT=CACHE, GIT_HTTP_EXPORT_ALL="1",
                   GIT_HTTP_MAX_REQUEST_BUFFER=str(FETCH_BODY),  # http-backend puffert upload-pack, default 10 MB
                   REQUEST_METHOD=self.command, PATH_INFO=url.path, QUERY_STRING=url.query,
                   CONTENT_TYPE=self.headers.get("Content-Type", ""), CONTENT_LENGTH=str(size),
                   REMOTE_USER=user or "anonymous", REMOTE_ADDR=self.client_address[0])
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        p = subprocess.Popen(["git", "http-backend"], stdin=body, stdout=subprocess.PIPE, env=env,
                             start_new_session=True)  # eigene prozessgruppe, siehe killpg unten
        try:
            status, headers = 200, []
            for line in iter(p.stdout.readline, b""):
                line = line.rstrip(b"\r\n")
                if not line:
                    break
                k, sep, v = line.decode("latin-1").partition(":")
                if not sep:
                    continue  # kaputte zeile ignorieren
                if k.lower() == "status":
                    status = int(v.split()[0])
                else:
                    headers.append((k, v.strip()))
            yield p.stdout, status, headers
        except BaseException:
            # SIGTERM statt SIGKILL: dann räumt http-backend sein upload-/receive-pack selbst ab und
            # sammelt es ein. SIGKILL träfe nur http-backend, das kind liefe weiter (counting objects …)
            # und würde als waise zum zombie, wenn wir pid 1 sind
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
            raise
        finally:
            p.stdout.close()
            p.wait()

    def reply(self, status, headers):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def handle_git(self):
        user, role, bad = self.login()
        if bad:
            return self.deny()
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/login" and self.command == "GET":
            return self.reply(303, [("Location", "/"), ("Content-Length", "0")]) if user else self.deny()
        if url.path == "/" and self.command == "GET":
            body = index_page(user)
            self.reply(200, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                             ("Cache-Control", "no-store")])
            self.wfile.write(body)
            return
        mg = GC_RE.fullmatch(url.path)
        if mg:  # POST /foo.git/gc: manuell, z.b. alle paar monate. braucht rw wie ein push
            if self.command != "POST":
                return self.text(405, "method not allowed")
            if not user:
                return self.deny()
            if role != "rw":
                return self.text(403, f"gits3: {user} darf nur lesen")
            return self.run_gc(mg[1])
        m = REPO_RE.fullmatch(url.path)
        if not m:
            return self.text(404, "not found")
        repo, sub = m[1], m[2]
        if (sub == "/info/refs") != (self.command == "GET"):
            return self.text(405, "method not allowed")
        service = sub[1:] if self.command == "POST" else urllib.parse.parse_qs(url.query).get("service", [""])[0]
        if service not in ("git-upload-pack", "git-receive-pack"):
            return self.text(403, "nur smart http")
        push = service == "git-receive-pack"
        if push and not user:
            return self.deny()
        if push and role != "rw":
            return self.text(403, f"gits3: {user} darf nur lesen")
        if not user and not is_public(repo):
            return self.deny()  # auch für nicht existierende repos: anonym verrät nichts über private namen

        with tempfile.TemporaryFile() as body:
            # GET hat keinen body. upload-pack ist anonym erreichbar und kriegt ein kleines limit,
            # nur push (rw-login) darf GITS3_MAX_BODY
            size = self.read_body(body, MAX_BODY if push else FETCH_BODY) if self.command == "POST" else 0
            if push and self.command == "POST":
                return self.receive(repo, url, body, size, user)
            # info/refs (beide services) und upload-pack: lesen nur, also geteilt. streamen, während
            # der cache festgehalten wird
            with synced(repo, push=False) as etag:
                if etag is None:
                    if not push:
                        return self.text(404, "no such repo")
                    empty_repo()  # push auf ein neues repo, angelegt wird es erst beim POST
                    url = url._replace(path=f"/{EMPTY}{sub}")
                with self.cgi(url, body, size, user) as (out, status, headers):
                    self.reply(status, headers)
                    shutil.copyfileobj(out, self.wfile, 1 << 16)

    def run_gc(self, repo):
        with synced(repo, push=True) as etag:  # push-lock: clones warten kurz, pushes ebenso
            if not etag:  # kein repo im bucket, synced hat nur einen leeren cache angelegt
                shutil.rmtree(local_dir(repo), ignore_errors=True)
                return self.text(404, "no such repo")
            msg = gc(repo, etag)
        self.text(200, f"gits3: {msg}")

    def receive(self, repo, url, body, size, user):
        with synced(repo, push=True) as etag:
            local = local_dir(repo)
            before_refs = read(os.path.join(local, "packed-refs"))
            before_packs = set(os.listdir(os.path.join(local, "objects", "pack")))
            with self.cgi(url, body, size, user) as (stdout, status, headers):
                out = stdout.read()
            # http 200 heißt nur "protokoll lief", nicht "push angenommen": push_up prüft die refs
            try:
                pushed = status == 200 and push_up(repo, etag, before_refs, before_packs)
            except Exception as e:
                self.log_error("push nach s3: %r", e)
                rollback(repo, before_refs, before_packs)  # wirft das selbst, verwirft synced() _checked
                return self.text(500, f"gits3: sync nach s3 fehlgeschlagen ({e}), nochmal pushen")
            if not pushed and not etag:
                shutil.rmtree(local)  # abgelehnter erster push: kein cache-dir für ein repo, das es nicht gibt
            elif not pushed:
                rollback(repo, before_refs, before_packs)  # abgelehnt: liegengebliebene packs weg
            else:
                forget_index()
                try:  # der push ist drin, ein krummer HEAD ist kein grund, ihn als fehler zu melden
                    fix_head(repo)
                except Exception as e:
                    self.log_error("HEAD nicht umgebogen: %r", e)
        self.reply(status, headers)
        self.wfile.write(out)


def check_git():
    """git und git-http-backend da? sonst merkt man es erst am ersten request."""
    try:
        r = subprocess.run(["git", "--exec-path"], env=GIT_ENV, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"gits3: git nicht ausführbar: {e}")
    if not os.access(os.path.join(r.stdout.strip(), "git-http-backend"), os.X_OK):
        sys.exit("gits3: git-http-backend fehlt (alpine: paket git-daemon)")


def check_backend():
    """kann das backend conditional writes und liefert es etags? sonst ist das cas auf packed-refs
    wirkungslos und instanzen überschreiben sich stumm. lieber gar nicht erst starten."""
    key = ".gits3-probe"  # liegt direkt unter GITS3_PREFIX, list_repos ignoriert es
    try:
        for _ in range(2):  # spätestens der zweite PUT muss 412 liefern
            try:
                s3.put(key, b"gits3\n", {"If-None-Match": "*"})
            except S3Error as e:
                if e.status != 412:
                    raise
                break
        else:
            sys.exit("gits3: s3-backend ignoriert If-None-Match: * (conditional writes), so geht kein cas")
        try:
            s3.put(key, b"gits3\n", {"If-Match": '"gits3-gibt-es-nicht"'})
        except S3Error as e:
            if e.status != 412:
                raise
        else:
            sys.exit("gits3: s3-backend ignoriert If-Match (conditional writes), so geht kein cas")
        st, h, _ = s3.get(key)
        if st != 200:
            raise S3Error(f"GET {key} -> {st}", st)
        if not h.get("ETag"):
            sys.exit("gits3: s3-backend liefert keinen ETag, ohne geht kein cas (If-Match)")
    except (S3Error, OSError) as e:
        sys.exit(f"gits3: s3-probe fehlgeschlagen: {e}")


def clean_cache():
    """beim start, also ohne parallele requests: lockfiles weg und caches von repos, die es im bucket
    nicht gibt (gelöscht, abgebrochener erster push). zur laufzeit lassen sich lockfiles nicht sicher
    löschen: wer schon auf den alten inode wartet, sperrt am neuen vorbei."""
    os.makedirs(CACHE, exist_ok=True)
    known = {r["name"] for r in list_repos()}
    for n in os.listdir(CACHE):
        path = os.path.join(CACHE, n)
        name, ext = os.path.splitext(n)
        if ext in (".lock", ".use") and REPO_NAME_RE.fullmatch(name):
            os.remove(path)
        elif REPO_NAME_RE.fullmatch(n) and n not in known and os.path.isdir(path):
            shutil.rmtree(path)
    init_local(EMPTY)


if __name__ == "__main__":
    check_git()
    check_backend()
    clean_cache()
    host, _, port = LISTEN.rpartition(":")
    host = host.strip("[]")  # [::1]:8000

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    print(f"gits3: http://{LISTEN} -> {s3.scheme}://{s3.host}{s3.base}/{s3.bucket}/{s3.prefix}", file=sys.stderr)
    Server((host, int(port)), Handler).serve_forever()
