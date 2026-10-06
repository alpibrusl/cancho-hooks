# The service for an AI agent: `hooks-mcp`

`hooks-mcp` lets an AI agent look into a running hooks service, and, if you allow it, send an event or an event again. It is an [MCP](https://modelcontextprotocol.io) server (the Model Context Protocol, revision 2025-06-18) that speaks over standard input and output, written in lex-sys like the service, and built with it: `build/hooks-mcp`. It makes ordinary requests to the service's HTTP API ([api.md](api.md)); it has no access to the service's files, and nothing it does is outside what that API, with the token you give it, would allow.

**Read-only unless you say otherwise.** Without `--allow-write` it offers six tools, all of which only read, and the tools that change something are not listed (a call to one is refused as an unknown tool). Creating, changing or removing endpoints, schedules, erasing an event, `/config` and `/metrics` are not offered with or without the flag (see "What an agent is not given").

## Starting it

```
hooks-mcp [--url http://HOST:PORT] [--token-file PATH] [--allow-write] [--allow-remote-plaintext] [--timeout-seconds N]
```

| flag | |
|---|---|
| `--url` | the service, `http://127.0.0.1:8080` by default. Plain HTTP only: a name or an IPv4 address, an optional port, no path. An `https` URL, an IPv6 address, credentials in the URL are refused at the start (exit 2) |
| `--token-file` | a file holding a bearer token for the service (its trailing whitespace is dropped; one token of visible ASCII). The token is never taken from the command line, so it is not in `ps`, and it is never written anywhere but the `Authorization` header of a request to `--url`: not on standard output, not on standard error, not in an error text |
| `--allow-write` | also offer `hooks_post_event`, `hooks_replay_event`, `hooks_replay_dead_letters` and `hooks_cancel_replay` |
| `--allow-remote-plaintext` | send the token to a host that is not this machine (see below) |
| `--timeout-seconds` | the most one call to the service may take, 30 by default, at most 600 |

**A token does not cross a network in the clear by accident.** The connection has no TLS. With `--token-file`, a `--url` whose host is not this machine (`localhost`, or an IPv4 address in `127.0.0.0/8`; a name that only begins with `127.` is not) is refused at the start with exit 2, unless `--allow-remote-plaintext` says that the network between the two is one you trust. Without a token, nothing secret is sent, and any host is allowed. Run it on the machine of the service, or reach the service through an SSH tunnel or a TLS-terminating proxy that listens on a local address.

Standard output carries the protocol and nothing else; diagnostics go to standard error. The end of standard input ends it (exit 0). A mistake in the command line is exit 2 and a message on standard error.

## Configuring an MCP client

Any client that starts an MCP server as a command takes a configuration of this shape (the file and the key names are the client's: this is the form most of them use):

```json
{
  "mcpServers": {
    "hooks": {
      "command": "/opt/hooks/bin/hooks-mcp",
      "args": ["--url", "http://127.0.0.1:8080", "--token-file", "/etc/hooks/mcp.token"]
    }
  }
}
```

To let the agent post and replay, add `"--allow-write"` to `args`. Give it a token of the scope it needs and no more ([security.md](security.md)): the read token for the read-only tools, the ingest token to post events, the admin token for the replays and for taking one back. One token file serves one run, so a client that does both has a file with a token that does both: that is the admin token, and whoever can read the file can do everything the admin token can, which is more than the four write tools. Read-only with the **read** token is the configuration with the least in it.

## The tools

| tool | what it does | scope of the service | needs `--allow-write` |
|---|---|---|---|
| `hooks_health` | `GET /readyz`: ready, or the check that fails | none | no |
| `hooks_stats` | `GET /stats`: the counters since the start | read | no |
| `hooks_get_event` | `GET /events/{id}`: one stored event (`event_id`) | read | no |
| `hooks_list_endpoints` | `GET /endpoints`: id, port, scheme, cursor, state, type patterns, header names, limits; never a host, a secret or a header's value (`limit`, `offset`) | read | no |
| `hooks_list_dead_letters` | `GET /endpoints/{id}/dead`: an endpoint's dead letters (`endpoint_id`; `limit`, `order`, `after`: the answer's `next` is the next `after`) | read | no |
| `hooks_get_attempts` | `GET /events/{id}/attempts`: the attempts of an event, from the database (`event_id`) | read | no |
| `hooks_post_event` | `POST /events`: a new event, `{"type": type, ...fields}` (`type`; `fields`, an object; `idempotency_key`) | ingest | yes |
| `hooks_replay_event` | `POST /events/{id}/replay[/{endpoint}]`: send a stored event again (`event_id`; `endpoint_id`) | admin | yes |
| `hooks_replay_dead_letters` | `POST /endpoints/{id}/replay-dead`: send an endpoint's dead letters again, oldest first (`endpoint_id`; `limit`, `types`, `after`) | admin | yes |
| `hooks_cancel_replay` | `DELETE /events/{id}/replay/{endpoint}`: take back a replay that has not been sent yet (`event_id`, `endpoint_id`) | admin | yes |

`tools/list` gives each tool's JSON schema, which is closed (`additionalProperties: false`), and a description written for the model. A result is one text content: the service's JSON body, byte for byte. A call that the service answers with anything but a `2xx` is a result with `isError: true` and the text `HTTP <status>: <the service's body>` (a 404 for an event that was never given, a 410 for one retention dropped or that was erased, a 503 for attempts when no database is named, a 401 for a token that is wrong). The same is said of a service that cannot be reached, that does not answer within the time, that answers something that is not HTTP/1, or whose answer is over 4 MiB.

**Arguments are judged before anything is sent.** Ids are integers from 1 to 2^53-1, `limit`, `offset` and `after` are in the service's own ranges, `order` is `asc` or `desc`, an event type is 1 to 200 characters without control characters, an idempotency key is 1 to 255 visible ASCII characters, the `fields` of an event are an object nested at most 16 deep without a `type` of its own, and an event is no larger than the service takes (65,499 bytes less 11 and the type's length, and 28 and the key's length when there is a key). A tool name that does not exist, an argument that is not in the schema or is there twice, or any value outside those is a JSON-RPC error `-32602` that names the argument, and no request is made. A path is built only from integers that passed, never from text of the client.

## What an agent is not given

- **Erasing an event** (`DELETE /events/{id}`): irreversible.
- **Creating, changing or removing endpoints** (and enabling one): their answers carry the secrets that sign the deliveries, and the changes redirect where events go.
- **Schedules**, **`/config`** and **`/metrics`**.

An agent that is to do those has the HTTP API and a token, as a person does. This tool is for looking, and for the two things that are safe to repeat (an event with an idempotency key, a replay that the service already bounds to 32 waiting).

**What the read tools show.** `hooks_get_event` returns the body of an event, which is whatever your producers sent: it may hold personal data, and it goes to the model of the client. An event that was erased answers 410. Whether that is acceptable for your events is a decision for you before you give an agent the tool.

## Limits

One message is a line of at most 1,048,576 bytes (a longer one is answered with `-32600` and read through, not kept); the answer of the service is read to 4 MiB; a call to the service takes at most `--timeout-seconds` in all, from the connect to the last byte of the answer. A name in `--url` is resolved by a call that waits and that the time limit does not cover (lex-sys #259): give an address. One request is made at a time (the messages of a batch are answered in order), each on a new connection.

## Authority

`lex-sys authority` reports that `hooks-mcp` performs `io_read`, `io_write` and `err_write` (standard input, output and error), `net_out` (a connection, to the host in `--url`: the report cannot bound it, because the host is an argument), `fs_read` (the token file, likewise an argument), `poll` and `clock` (the time limit), and `heap` and `args`; that it is bounded, and that it calls **no foreign function** (the service calls 32, of OpenSSL). The report is pinned in [authority-mcp.json](authority-mcp.json) and checked in CI. What the report does not say is which host: that is the program, and the tests (`tests/mcp_test.py`) are what show that it connects to the one in `--url` and sends the token to no other place. The reasoning, the measurements and what is not claimed are in [design.md](design.md) section 51.
