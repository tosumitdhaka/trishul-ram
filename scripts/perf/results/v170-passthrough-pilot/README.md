# v1.7.0 Pilot B: validated same-schema Protobuf passthrough campaign

Image: local-20261007111937 = release/v1.7.0 @ http_accel Dockerfile commit (uvloop 0.23.0 in-image).
Method: identical to the v170-protobuf-anchor run (kind, mw, 500m workers, fsweep corpus 10x10k .pb,
5 reps; run_ab.py side=v170 scenario=fsweep_pt; template fsweep_protobuf_passthrough.yaml sets
protobuf_passthrough: true; PERF_PROTO_SCHEMA override for host-side template validation — pod
runtime uses /data/schemas/cdr.proto, manager pod staged for registration-time content-hash).

## Results (all reps 100k in / 100k out, 0 errors, 0 skipped)
| rep | wall_s | rec/s |
|---|---|---|
| 1 | 0.4 | 250,000 |
| 2-5 | 0.3 | 333,333 |

**Median 333,333 rec/s = 30x the 11,111 v1.6.1 anchor** (target was 2x / ~22.2k). Peak worker RSS
151Mi vs 247Mi on the dictionary path. **Byte preservation proven**: sha256 of concatenated
/data/perf/in/*.pb == /data/perf/out/*.pb == 2515de6147975a41fdfff179bf7ede81570256b2a6cdab1ced923acd0ad76899.

## Notes
- duration_s is the same started_at->finished_at wall metric as the anchor runs (9.0s there).
- The dictionary path cannot produce 0.3s (90us/record vs 3us/record) — the fast path engaged.
- Eligibility required staging cdr.proto on the manager pod (registration-time schema content hash).
