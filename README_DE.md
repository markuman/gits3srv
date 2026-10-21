<div align="center">

# 🐚 gits3

**Ein git-server in einer datei. Clone und push über HTTP — gespeichert in S3.**

*Eine datei · Kein framework · Keine datenbank · Kein pip*

[![Python ≥ 3.9](https://img.shields.io/badge/Python-%E2%89%A53.9-3776ab)](https://www.python.org)
[![git ≥ 2.32](https://img.shields.io/badge/git-%E2%89%A52.32-f05032)](https://git-scm.com)
[![S3](https://img.shields.io/badge/storage-S3-569A31)](https://de.wikipedia.org/wiki/Amazon_S3)

<br>

```
git client ──http──▶ gits3.py ──s3──▶ bucket (aws, minio, nextcloud-s3-api, …)
                        └─ lokaler cache (wegwerfbar)
```

[english](README.md)

</div>

---

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

`/` zeigt alle repos mit suche und sortierung. ein klick kopiert den clone-befehl, ein klick auf `readme.md` zeigt rechts die `README.md` aus `HEAD` (roh: `GET /foo.git/readme`). markdown für arme: überschriften, fett, kursiv, links, aufzählungen. tabellen & co. bleiben quelltext.

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

## große repos auf schmaler hardware

**tl;dr:** vor dem ersten push auf starker hardware aggressiv repacken. das pack bleibt dauerhaft kleiner, weil `/gc` die deltas nur übernimmt.

- ein push ist ein request mit einem pack. gits3 legt es 1:1 als **einen** PUT in den bucket
- `/gc` macht `repack -a -d` **ohne** `-f`: das ist billig (keine delta-suche, einkernig), optimiert aber auch nichts
- also die teure delta-suche einmal lokal machen, dann pushen:

```bash
git clone --bare --single-branch --branch main https://quelle/foo.git x.git
git -C x.git repack -a -d -f --window=250 --depth=50   # alle kerne, minuten. nicht auf dem pi
git -C x.git push https://git.example.com/foo.git main
```

- jedes glied der kette braucht ein body-limit ≥ pack-größe und timeouts, die copy + hash danach aushalten:
  - proxy vor gits3: `client_max_body_size`, `proxy_read_timeout`
  - `GITS3_MAX_BODY`
  - proxy vor dem s3, bei nextcloud `client_max_body_size` und `fastcgi_read_timeout`
- das s3-limit großzügiger als `GITS3_MAX_BODY` wählen: `/gc` schreibt ein pack, das kein push-limit kennt und mit dem repo wächst

**zahlen** (pdf-lastiges repo, `main`, 1,26 mio. objekte):

| | ergebnis |
|---|---|
| pack wie geklont | 1,95 GB |
| `repack -f --window=250 --depth=50` | 1,60 GB (−18 %), 468 s auf 16 threads (ryzen) |
| `/gc` (`repack -a -d`, 4 packs → 1) | 12 s, ein kern (ryzen) |
| push auf raspberry pi 5 mit nextcloud-s3 | läuft, sobald dort `client_max_body_size` (default 512M) und `fastcgi_read_timeout` (default 60 s) hochgesetzt sind |

## grenzen

- **gc nur manuell.** jeder push erzeugt ein neues pack. `POST /foo.git/gc` (rw-login) repackt alles zu einem pack und löscht die alten aus dem bucket. unerreichbare objekte (z.b. gelöschte branches) fliegen dabei raus. der speicher bleibt im papierkorb der nextcloud, bis er leer ist.
- **ein pack = ein PUT.** sehr große packs scheitern am upload-limit des s3-servers, siehe [große repos](#große-repos-auf-schmaler-hardware).
- **eine instanz pro bucket/prefix.** nur die sperrt sauber per `flock`. rolling- und blue/green-deploys zählen als zwei.
- **cas ist nur so gut wie der etag.** der etag muss sich mit dem inhalt ändern (md5 wie bei aws oder nextcloud mit s3_api ≥ 1.2.0). sekundengenaue etags (nextclouds eigener) können bei zwei instanzen pushes verschlucken.
- **gc gegen eine zweite instanz.** vor dem löschen alter packs schreibt gc `packed-refs` per cas neu, mit token im header (`gits3-gc-<ts>`, git ignoriert ihn). ein paralleler push bekommt 412, statt sein pack zu verlieren. identischer inhalt reicht nicht: der md5-etag bliebe gleich.
- **verwaiste packs.** scheitert das cas nach dem pack-upload, bleibt das pack ohne ref im bucket bis zum nächsten gc.
- **`receive.fsckObjects`.** alte repos mit fsck-macken (z.b. `zeroPaddedFilemode`) lassen sich u.u. nicht pushen.
- **`HEAD`.** zeigt auf `main`. fehlt der branch, biegt gits3 `HEAD` beim push auf einen vorhandenen um. ändert sich `HEAD` im bucket von außen, sieht der cache das erst mit der nächsten änderung an `packed-refs`.
- **langsame leser.** ein clone sperrt das repo geteilt, ein push wartet bis zu dessen ende.
- **gc blockiert.** gc hält den exklusiven lock. das repack großer repos dauert minuten, clones und pushes warten so lange. am reverse-proxy `proxy_read_timeout` (nginx) bzw. das timeout entsprechend hochsetzen.

---

<div align="center">
*Designed by a human, generated by robots. Blame the human.*
</div>
