// Bounded World metadata: absent pending_export_requests means {}, absent revision means 0.
// Values are written only by the reminder WorldWriter; no bulk state repair is required.
RETURN 1 AS pending_exports_schema;
