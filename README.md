# gits3

git-server in einer datei: clone und push über http, gespeichert wird in s3.

```
git client ──http──▶ gits3.py ──s3──▶ bucket (aws, minio, nextcloud-s3-api, …)
                        └─ lokaler cache (wegwerfbar)
```

## prinzipien

- **eine datei.** python-stdlib und `git`. kein pip, keine db, kein framework.
- **der bucket ist die wahrheit.** der cache ist wegwerfbar, löschen heißt neu syncen.
- **der bucket ist ein bare repo.** wiederherstellen geht mit `git clone`, ohne eigenes format.
- **git macht die arbeit.** das protokoll spricht `git http-backend`, gits3 schiebt nur dateien.
- **append-only.** packs werden nie geändert. einzig `packed-refs` ändert sich, per cas (`If-Match`).
- **sicher per default.** repos sind privat, push nur mit login, git läuft abgeschottet.
- **fail fast.** ohne `git-http-backend`, conditional writes oder etag startet gits3 gar nicht erst.
- **was fehlt, fehlt absichtlich.** keine issues, keine pull requests, keine webhooks, keine user-verwaltung.

## voraussetzungen

- python ≥ 3.9, git ≥ 2.32 (alpine: zusätzlich `git-daemon`)
- s3 mit path-style, etags und conditional writes (`If-Match`, `If-None-Match: *`)

## start

```bash
GITS3_ENDPOINT=https://cloud.example.com/apps/s3_api/s3 \
GITS3_BUCKET=git GITS3_PREFIX=repos/ \
AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… \
GITS3_USERS="m:pw:rw agent:pw2:ro" \
python3 gits3.py
```

oder als container:

```bash
docker build -t gits3 .
docker run -d -p 127.0.0.1:8000:8000 --env-file gits3.env -v gits3-cache:/cache gits3
```

```bash
git push http://localhost:8000/neu.git main   # erster angenommener push legt das repo an
git clone http://localhost:8000/neu.git
```

`/` zeigt alle repos mit suche und sortierung. ein klick kopiert den clone-befehl.

## neues repo

```bash
git init -b main && git add -A && git commit -m init   # ohne commit kein push
git remote add origin https://git.example.com/foo.git  # name: [A-Za-z0-9][A-Za-z0-9._-]*.git
git push -u origin main                                # login mit rw-user, legt das repo an
```

anlegen im bucket oder in der web-ui gibt's nicht, der erste push ist das anlegen. das repo ist erstmal privat.

## konfiguration

| variable | bedeutung | default |
|---|---|---|
| `GITS3_ENDPOINT` | s3-endpoint (path-style) | – |
| `GITS3_BUCKET` | bucket | – |
| `GITS3_PREFIX` | pfad im bucket | leer |
| `GITS3_REGION` | region für die signatur | `us-east-1` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | s3-key, am besten nur für diesen bucket | – |
| `GITS3_USERS` | `user:passwort:ro\|rw`, durch leerzeichen getrennt | leer |
| `GITS3_AUTH` | veraltet: `user:passwort`, zählt als `rw` | leer |
| `GITS3_CACHE` | lokaler cache | `./cache` |
| `GITS3_LISTEN` | adresse:port, auch `[::1]:8000` | `127.0.0.1:8000` |
| `GITS3_MAX_BODY` | max. push-größe in bytes | 2 GiB |
| `GITS3_INDEX_TTL` | cache der repo-liste in sekunden | `30` |

## zugriff

|   | öffentlich | privat |
|---|---|---|
| anonym | lesen | – |
| `ro` | lesen | lesen |
| `rw` | lesen, schreiben | lesen, schreiben |

öffentlich = im bucket liegt `foo.git/git-daemon-export-ok` (git-konvention). datei anlegen oder löschen schaltet um.

## layout im bucket

```
repos/foo.git/
  HEAD, config
  packed-refs              # alle refs, per cas
  git-daemon-export-ok     # optional: öffentlich
  refs/.keep               # s3 kennt keine leeren ordner
  objects/info/.keep
  objects/pack/pack-*.{pack,idx}
```

schreibreihenfolge: pack, idx, `packed-refs` zuletzt. ein repo existiert erst ab `packed-refs`. abgebrochene, abgelehnte und `--dry-run`-pushes hinterlassen nichts.

## wiederherstellung

```bash
aws s3 sync s3://git/repos/foo.git ./foo.git
git clone ./foo.git
```

`git fsck` direkt auf dem dump stolpert bei neuerem git über `refs/.keep`, deshalb `git fsck --no-references`.

## betrieb

- tls macht ein reverse proxy davor, bei nginx mit `client_max_body_size 0;`
- limits: push `GITS3_MAX_BODY`, clone/fetch 64 MiB (auch anonym), 300 s socket-timeout
- threads sind unbegrenzt: gegen viele verbindungen hilft der proxy (`limit_conn`)
- request-bodys landen in `$TMPDIR`, dort platz für parallele pushes einplanen
- git läuft ohne user-/system-config, ohne hooks und mit env-whitelist, sieht also keine secrets
- `safe.directory=*`, der cache darf einer anderen uid gehören
- ein cache gehört genau einer instanz. beim start fliegen lockfiles und caches gelöschter repos raus
- transiente s3-fehler werden zweimal wiederholt
- zugangsdaten gehören in eine env-datei, nicht ins repo

## grenzen

- **kein gc.** jeder push erzeugt ein neues pack.
- **ein pack = ein PUT.** sehr große packs scheitern am upload-limit des s3-servers.
- **cas ist nur so gut wie der etag.** sekundengenaue etags (nextcloud) können bei mehreren instanzen pushes verschlucken. eine einzelne instanz sperrt per `flock`.
- **verwaiste packs.** scheitert das cas nach dem pack-upload, bleibt das pack ohne ref im bucket liegen.
- **`receive.fsckObjects`.** alte repos mit fsck-macken (z.b. `zeroPaddedFilemode`) lassen sich u.u. nicht pushen.
- **`HEAD`.** zeigt auf `main`. fehlt der branch, biegt gits3 `HEAD` beim push auf einen vorhandenen um. ändert sich `HEAD` im bucket von außen, sieht der cache das erst mit der nächsten änderung an `packed-refs`.
- **langsame leser.** ein clone sperrt das repo geteilt, ein push wartet bis zu dessen ende.
