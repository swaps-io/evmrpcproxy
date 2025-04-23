ERP: Ethereum (Virtual Machine) Remote (Procedure Call) Proxy


## Usage ##

[Install uv](https://docs.astral.sh/uv/getting-started/installation/),
run `uv run evmrpcproxy api`

Alternatively, use docker:

    docker build -t evmrpcproxy . && docker run --rm -p 13431:13431 evmrpcproxy


### Example endpoints ###

EVMRPC proxy:
`http://127.0.0.1:13431/api/v1/evmrpc/{chain_id_or_name}?x_requester={codebase_name}&token={auth_token}`
http://127.0.0.1:13431/api/v1/evmrpc/1?x_requester=test&token=xlocalonlyauthtoken

EVMRPC check:
`curl -Ss -X POST "http://127.0.0.1:13431/api/v1/evmrpc_check/?token=${EVMRPC_PROXY_TOKEN}" | jq .`

`curl -Ss -X POST "http://127.0.0.1:13431/api/v1/evmrpc_check/?token=xlocalonlyauthtoken" | jq .`


## Development ##

Before making a commit, run `uv run hyd`


## See also ##

A more caching less processing ERP in Go: https://github.com/erpc/erpc
