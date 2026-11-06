# MultiVocale — reservation pre-download

## Invariants

1. The webhook reserves batch capacity before the downloader reads the first audio-body chunk.
2. A valid `Content-Length` is the initial reservation. If the header is absent, the downloader reserves `MAX_FILE_BYTES` before reading.
3. If the body contradicts the advertised length, the reservation is expanded atomically before the additional bytes are appended to the in-memory buffer.
4. When the download completes, the reservation is reduced to the actual byte count before upload.
5. The Redis mutation accounts for ready bytes and all other in-flight reservations in one WATCH/MULTI transaction. A rejected resize leaves the previous reservation intact.
6. Every download failure calls `fail_audio`, which releases the in-flight reservation. A successful completion converts the reservation into ready-byte accounting.

## Memory boundary

The reservation protects the aggregate byte budget of one MultiVocale batch across concurrent webhook handlers. Each individual download is independently capped by `MAX_FILE_BYTES` and read in 64 KiB chunks. This is a batch-level guard, not a global process-wide RAM quota across different senders or batches.

## Regression coverage

- Reservation callback is invoked before the first body read.
- Missing `Content-Length` reserves the per-file maximum.
- An inaccurate small `Content-Length` causes an atomic expansion before retaining the extra chunk.
- Reservations can shrink to actual size and expand when capacity permits.
- A rejected expansion preserves the existing reservation.
- Existing Redis concurrency tests continue to check that ready plus in-flight bytes never exceed the batch limit.
