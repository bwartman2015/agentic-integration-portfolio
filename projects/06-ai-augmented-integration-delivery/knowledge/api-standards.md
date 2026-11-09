# Ridgeline integration API standards (synthetic)

- Versioned base path `/v1/`; breaking changes need a new version.
- Every operation declares a security scheme (OAuth2 client credentials).
- Standard error model `{code, message, correlation_id}`.
- List endpoints must paginate (`limit` <= 200, `cursor`).
- Writes accept an `Idempotency-Key` header.
- Outbound calls set timeouts (connect 2s, read 10s) and retry only idempotent operations.
- Never log full payloads containing customer contact data.
