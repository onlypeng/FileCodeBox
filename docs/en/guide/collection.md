# Inbox (File Collection)

An inbox is the reverse of a normal share: instead of sending a file to one
recipient, you collect files from many people.

## Codes

Creating an inbox yields separate codes, and the separation is the point — the
person submitting a file must not be able to read everyone else's submissions.

| Code | Given to | Can |
|------|----------|-----|
| Delivery code | Submitters | Upload files only |
| Retrieval code | Collector | View and download submissions |
| Manage code | Collector | Change settings, delete submissions |
| `manage-by-delivery` | — | Look up manage info via the delivery code |

## Creating and configuring

Use `POST /collection/create/`, then adjust settings with
`PATCH /collection/update/{collection_code}`:

- maximum number of files (`maxCollectionFiles`, default 100;
  `collectionDefaultMaxFiles`, default 20)
- expiry (`collectionDefaultExpireDays`, default 7 days)
- title / description shown on the delivery page

## Submitting

`POST /delivery/upload/` handles a single-request upload. Large files use the
chunked path: `POST /delivery/chunk/init/` → upload chunks →
`POST /delivery/chunk/complete/` (or `/chunk/cancel/` to abort).

`GET /delivery/{delivery_code}` returns the information the delivery page needs.

## Collecting

- `GET /collection/retrieve/{retrieve_code}` — submission list for recipients
- `GET /collection/manage/{collection_code}` — full management view
- `GET /collection/download/{file_id}` — download one submission
- `GET /collection/zip/{code}` — download everything as a ZIP
- `DELETE /collection/delete/{file_id}` — remove one submission
- `GET /collection/status/{collection_code}` — status

The admin panel also provides inbox management under `/admin/collection/*`.

## API details

See the [API Reference](/en/api/#inbox-collection-and-delivery-delivery) for the
complete route table and parameter conventions.
