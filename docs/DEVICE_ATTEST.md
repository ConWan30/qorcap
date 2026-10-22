# Device attestation sidecar

`qorcap device-key` creates an opt-in signing key and can write `device_attest.v0.json` next to a match receipt. `receipt.json` is not modified. `receipt.v0.json` rejects unknown fields, so this object is a sidecar.

The command does not open a gamepad and does not send output or feature reports.

```
qorcap device-key init [--bundle DIR] [--software] [--key-dir DIR]
qorcap device-key status [--bundle DIR] [--key-dir DIR]
```

`init` creates the key if it does not exist. With `--bundle`, it also writes the sidecar from that folder's `receipt.json`. `status` prints the key record and, with `--bundle`, checks the sidecar. The default key directory is the same one QorCap already uses (`QORCAP_HOME/keys`, otherwise `%LOCALAPPDATA%\QorCap\keys` on Windows, otherwise `~/.qorcap/keys`).

## Which key

On Windows, `init` opens CNG provider `Microsoft Platform Crypto Provider` with `NCRYPT_SILENT_FLAG`. It creates a persisted user key named `QorCap-Device-Attest-v0`.

- ECDSA P-256 when `NCryptIsAlgSupported` accepts `ECDSA_P256`.
- Otherwise RSA-2048 (`Length` set to 2048 before finalize).

`NCryptIsAlgSupported` and `NCryptDeleteKey` are called with flags 0. This provider returns `NTE_BAD_FLAGS` (`0x80090009`) when those two calls are passed `NCRYPT_SILENT_FLAG` (`0x40`), so a silent algorithm check never reaches key creation and a silent delete leaves the key behind. `NCryptOpenStorageProvider`, `NCryptOpenKey`, and `NCryptFinalizeKey` still use the silent flag. If neither algorithm is created, the software-key fallback records each call's HRESULT (`ECDSA_P256: NCryptIsAlgSupported returned 0x80090009; RSA: ...`).

Before finalize, `Export Policy` is set to 0 (no export, plaintext-export, or archiving bits). The private key is not written to a file. A later `init` opens that same key. It does not overwrite it.

If the provider does not open, or key creation fails, `init` falls back to a software key and records the reason in the local key file `device-attest-v0.key.json`. The sidecar's `key_provider` is then `software`. `--software` skips the TPM even when the provider opens. An existing TPM key is left in place.

On any other OS the command uses the software key. The TPM provider is Windows-only.

The software key is a P-256 PKCS#8 file, `device-attest-v0.p256.pkcs8`, in the key directory. The file mode is `0600` on Unix. On Windows the ACL is rewritten with `icacls` so only the current user has access, the same treatment as the other QorCap private keys. `non_exportable` is false: the file can be copied.

## Bytes that are signed

The message is 89 bytes. It is the ASCII text

```
qorcap-device-attest/v0
<64 lowercase hex characters of the final checkpoint hash>
```

including the newline after the prefix and the newline after the hex. That is the same byte string the QorTime referee verifies.

The final checkpoint hash is `receipt.checkpoints[role][-1].hash`, where `role` is the receipt player (`players[0].role`) when that chain is non-empty. If the receipt has no player role, it is the only non-empty `home` or `away` chain. Both chains and no player role is an error. This hash is the checkpoint hash (`SHA-256` of the checkpoint body). It is not the video segment `chain_head`.

ECDSA P-256 signs SHA-256 of those 89 bytes. The sidecar stores the signature as DER, standard base64. RSA-2048 signs the same SHA-256 with RSASSA-PKCS1-v1_5. That signature is 256 bytes, standard base64. The public key is a SubjectPublicKeyInfo, DER, standard base64, stored as both `public_key_spki_b64` and `public_key_spki_der_b64`.

`key_provider`, `non_exportable`, and the `tpm` object are not part of the signed message. Changing them does not make the signature fail. A verifier that only checks the signature cannot trust those fields.

## Sidecar fields

| Field | Meaning |
|---|---|
| `schema` | `qorcap-device-attest/v0` |
| `key_provider` | `platform-crypto` for the Windows TPM key, or `software`. A label. Not signed. The CNG provider that was opened is still `Microsoft Platform Crypto Provider`. |
| `algorithm` | `ECDSA_P256` or `RSA_2048`. The referee accepts `ECDSA_P256` as `ecdsa-p256-sha256`. It does not verify `RSA_2048`. |
| `public_key_spki_b64` | SPKI DER, standard base64. The name the referee reads. |
| `public_key_spki_der_b64` | The same SPKI. |
| `non_exportable` | See below. `null` if the provider did not report a policy. Not signed. |
| `chain_head` | The checkpoint hash that was signed. Verification recomputes it from the receipt and requires the two to match. |
| `signature_b64` | Signature over the 89 bytes above. P-256 is DER. |
| `tpm` | Optional. Omitted when the provider returned nothing. |

`tpm.key_attestation_b64` is the blob from `NCryptCreateClaim` when that export exists and the call succeeds. `tpm.key_attestation_claim` is `NCRYPT_CLAIM_PLATFORM` (`0x00010000`) or, if that claim type fails, `NCRYPT_CLAIM_SUBJECT_ONLY` (`0x00000002`). The call is loaded with `GetProcAddress`. A missing export does not stop `init`.

`tpm.ek_cert_b64` is the raw property value of `PCP_ECC_EKNVCERT`, `PCP_RSA_EKNVCERT`, or `PCP_EKNVCERT`, whichever returns at least 64 bytes. Those properties are the endorsement certificate in TPM NV. `PCP_EKCERT` is an `HCERTSTORE` handle, not certificate bytes, and is not stored. `tpm.platform_type` is the provider string `PCP_PLATFORM_TYPE` when it returns one.

## What this proves

A signature that verifies proves that the private key for `public_key_spki_b64` signed that one checkpoint hash.

`non_exportable: true` means that, when the value was read, CNG `Export Policy` had none of `NCRYPT_ALLOW_EXPORT_FLAG` (`0x1`), `NCRYPT_ALLOW_PLAINTEXT_EXPORT_FLAG` (`0x2`), `NCRYPT_ALLOW_ARCHIVING_FLAG` (`0x4`), or `NCRYPT_ALLOW_PLAINTEXT_ARCHIVING_FLAG` (`0x8`). For a software key the field is false.

`tpm_attestation: verified` in `device-key status` means this machine opened `QorCap-Device-Attest-v0`, its public key matched the sidecar, and `NCryptVerifyClaim` returned success for the recorded claim type. For `NCRYPT_CLAIM_PLATFORM`, Windows accepted the blob as a platform claim for that Platform Crypto Provider key: the key was created in that TPM and the provider is not handing out an exportable software copy. The check runs only on the machine that holds the key.

## What this does not prove

- It does not prove QorCap, ffmpeg, or any other program on the machine was unmodified.
- It does not prove the frames came from HDMI, or that they were hashed before a PC could replace them.
- It does not prove who held the controller.
- An unverified attestation blob is carried, not proof. `status` reports `present_unverified` when the blob is there but `NCryptVerifyClaim` was not run or did not succeed. The receipt signature is still checked on its own.
- The EK certificate is not the signing key. This build does not chain it to a TPM manufacturer CA. It does not prove the device key is the endorsement key.
- `key_provider` and `non_exportable` in the file can be edited without breaking the signature. `status` re-reads the live export policy only when the local TPM key's public key matches the sidecar, and reports that separately as `non_exportable_live`.
- A software key proves only that whoever has the PKCS#8 file signed the hash. Copy the file and the same signature can be produced on another machine.
