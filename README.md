<div align="center">

# 🐚 gits3

**A git server in a single file. Clone and push over HTTP — storage in S3.**

*One file · No framework · No database · No pip*

[![Python ≥ 3.9](https://img.shields.io/badge/Python-%E2%89%A53.9-3776ab)](https://www.python.org)
[![git ≥ 2.32](https://img.shields.io/badge/git-%E2%89%A52.32-f05032)](https://git-scm.com)
[![S3](https://img.shields.io/badge/storage-S3-569A31)](https://en.wikipedia.org/wiki/Amazon_S3)

<br>

```
git client ──http──▶ gits3.py ──s3──▶ bucket (aws, minio, nextcloud-s3-api, …)
                        └─ local cache (disposable)
```

[deutsch](README_DE.md)

</div>

---

## Principles

- **One file.** Python stdlib and `git`. No pip, no database, no framework.
- **The bucket is the source of truth.** The cache is disposable, deleting it just means a fresh sync.
- **The bucket is a bare repo.** Restore with `git clone`, no custom format.
- **git does the work.** The protocol is handled by `git http-backend`, gits3 only moves files.
- **Append-only.** Packs are never modified. Only `packed-refs` changes, via CAS (`If-Match`).
- **Secure by default.** Repos are private, push requires login, git runs sandboxed.
- **Fail fast.** Without `git-http-backend`, conditional writes or ETags, gits3 refuses to start.
- **Missing on purpose.** No issues, no pull requests, no webhooks, no user management.

## Requirements

- Python ≥ 3.9, git ≥ 2.32 (Alpine: also `git-daemon`)
- S3 with path-style addressing, ETags and conditional writes (`If-Match`, `If-None-Match: *`)

## Start

```bash
GITS3_ENDPOINT=https://cloud.example.com/apps/s3_api/s3 \
GITS3_BUCKET=git GITS3_PREFIX=repos/ \
AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… \
GITS3_USERS="m:pw:rw agent:pw2:ro" \
python3 gits3.py
```

or as a container:

```bash
docker build -t gits3 .
docker run -d -p 127.0.0.1:8000:8000 --env-file gits3.env -v gits3-cache:/cache gits3
```

```bash
git push http://localhost:8000/new.git main   # the first accepted push creates the repo
git clone http://localhost:8000/new.git
```

`/` lists all repos with search and sorting. One click copies the clone command, a click on `readme.md` shows the `README.md` from `HEAD` on the right (raw: `GET /foo.git/readme`). Poor man's markdown: headings, bold, italics, links, lists. Tables and the like stay as source.

## New repo

```bash
git init -b main && git add -A && git commit -m init   # no commit, no push
git remote add origin https://git.example.com/foo.git  # name: [A-Za-z0-9][A-Za-z0-9._-]*.git
git push -u origin main                                # login as rw user, creates the repo
```

There is no way to create a repo in the bucket or the web UI, the first push creates it. New repos are private.

## Configuration

| variable | meaning | default |
|---|---|---|
| `GITS3_ENDPOINT` | S3 endpoint (path-style) | – |
| `GITS3_BUCKET` | bucket | – |
| `GITS3_PREFIX` | path inside the bucket | empty |
| `GITS3_REGION` | region for the signature | `us-east-1` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | S3 key, ideally scoped to this bucket | – |
| `GITS3_USERS` | `user:password:ro\|rw`, space-separated | empty |
| `GITS3_AUTH` | deprecated: `user:password`, counts as `rw` | empty |
| `GITS3_CACHE` | local cache | `./cache` |
| `GITS3_LISTEN` | address:port, also `[::1]:8000` | `127.0.0.1:8000` |
| `GITS3_MAX_BODY` | max. push size in bytes | 2 GiB |
| `GITS3_INDEX_TTL` | repo list cache in seconds | `30` |

## Access

|   | public | private |
|---|---|---|
| anonymous | read | – |
| `ro` | read | read |
| `rw` | read, write | read, write |

Public = the bucket contains `foo.git/git-daemon-export-ok` (git convention). Creating or deleting that file toggles it.

## Bucket layout

```
repos/foo.git/
  HEAD, config
  packed-refs              # all refs, via CAS
  git-daemon-export-ok     # optional: public
  refs/.keep               # S3 has no empty folders
  objects/info/.keep
  objects/pack/pack-*.{pack,idx}
```

Write order: pack, idx, `packed-refs` last. A repo only exists once `packed-refs` exists. Aborted, rejected and `--dry-run` pushes leave nothing behind.

## Restore

```bash
aws s3 sync s3://git/repos/foo.git ./foo.git
git clone ./foo.git
```

`git fsck` directly on the dump trips over `refs/.keep` with newer git, so use `git fsck --no-references`.

## Operations

- TLS is done by a reverse proxy in front, for nginx with `client_max_body_size 0;`
- Limits: push `GITS3_MAX_BODY`, clone/fetch 64 MiB (anonymous too), 300 s socket timeout
- Threads are unbounded: the proxy protects against many connections (`limit_conn`)
- Request bodies go to `$TMPDIR`, plan space there for parallel pushes
- git runs without user/system config, without hooks and with an env whitelist, so it sees no secrets
- `safe.directory=*`, the cache may be owned by a different uid
- One cache belongs to exactly one instance. On start, lock files and caches of deleted repos are removed
- Transient S3 errors are retried twice
- Credentials belong in an env file, not in the repo

## Large repos on small hardware

**tl;dr:** Repack aggressively on strong hardware before the first push. The pack stays smaller for good, because `/gc` only reuses existing deltas.

- A push is one request with one pack. gits3 stores it 1:1 as **one** PUT in the bucket
- `/gc` runs `repack -a -d` **without** `-f`: cheap (no delta search, single core), but it doesn't optimize anything either
- So do the expensive delta search once locally, then push:

```bash
git clone --bare --single-branch --branch main https://source/foo.git x.git
git -C x.git repack -a -d -f --window=250 --depth=50   # all cores, minutes. not on the pi
git -C x.git push https://git.example.com/foo.git main
```

- Every link in the chain needs a body limit ≥ pack size, and timeouts that survive the copy + hash afterwards:
  - proxy in front of gits3: `client_max_body_size`, `proxy_read_timeout`
  - `GITS3_MAX_BODY`
  - proxy in front of S3, for Nextcloud `client_max_body_size` and `fastcgi_read_timeout`
- Set the S3 limit higher than `GITS3_MAX_BODY`: `/gc` writes a pack that knows no push limit and grows with the repo

**Numbers** (PDF-heavy repo, `main`, 1.26 M objects):

| | result |
|---|---|
| pack as cloned | 1.95 GB |
| `repack -f --window=250 --depth=50` | 1.60 GB (−18 %), 468 s on 16 threads (Ryzen) |
| `/gc` (`repack -a -d`, 4 packs → 1) | 12 s, single core (Ryzen) |
| push to a Raspberry Pi 5 with Nextcloud S3 | works once `client_max_body_size` (default 512M) and `fastcgi_read_timeout` (default 60 s) are raised there |

## Limitations

- **gc is manual only.** Every push creates a new pack. `POST /foo.git/gc` (rw login) repacks everything into one pack and deletes the old ones from the bucket. Unreachable objects (e.g. deleted branches) are dropped. With Nextcloud the space stays in the trash bin until it is emptied.
- **One pack = one PUT.** Very large packs fail at the S3 server's upload limit, see [large repos](#large-repos-on-small-hardware).
- **One instance per bucket/prefix.** Only that one locks properly via `flock`. Rolling and blue/green deploys count as two.
- **CAS is only as good as the ETag.** The ETag has to change with the content (MD5 like AWS, or Nextcloud with s3_api ≥ 1.2.0). One-second ETags (Nextcloud's own) can swallow pushes with two instances.
- **gc vs. a second instance.** Before deleting old packs, gc rewrites `packed-refs` via CAS with a token in the header (`gits3-gc-<ts>`, git ignores it). A parallel push gets 412 instead of losing its pack. Identical content would not do: the MD5 ETag would stay the same.
- **Orphaned packs.** If the CAS fails after the pack upload, the pack stays in the bucket without a ref until the next gc.
- **`receive.fsckObjects`.** Old repos with fsck issues (e.g. `zeroPaddedFilemode`) may not be pushable.
- **`HEAD`.** Points to `main`. If that branch is missing, gits3 repoints `HEAD` to an existing one on push. If `HEAD` is changed in the bucket from outside, the cache only notices with the next change to `packed-refs`.
- **Slow readers.** A clone holds a shared lock on the repo, a push waits until it finishes.
- **gc blocks.** gc holds the exclusive lock. Repacking large repos takes minutes, clones and pushes wait meanwhile. Raise `proxy_read_timeout` (nginx) or the equivalent timeout on the reverse proxy accordingly.

---

<div align="center">
*Designed by a human, generated by robots. Blame the human.*
</div>
