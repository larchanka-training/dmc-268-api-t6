# SEC-01 oracle

The added Python module makes the `FAKE_TEST_CREDENTIAL_DO_NOT_USE` constant the accepted token for `can_read_private_records`. The value is deliberately unusable outside this synthetic case, but it is functional inside the changed code and visible to anyone who can read the repository. The added line 3 is the single new-side anchor. A review should identify this hardcoded authorization secret; merely saying that the name looks fake misses the access-control failure.

Ground truth is `security` / `critical`: the function guards private partner records, and a caller can use the published constant to pass its check. This yields the derived `blocking` verdict. The base contains only the service description; the proposed PR adds the authentication module. No real credential or third-party source is included.
