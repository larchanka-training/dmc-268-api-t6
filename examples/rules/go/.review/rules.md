Template for Go services; adapt to your project: the paths `cmd/`, `internal/transport/`, `internal/service/`, the masks at the end.

# Architecture

## Layers

Packages under `cmd/` only wire dependencies and start the program. HTTP handlers in
`internal/transport/` decode the request, call `internal/service/` and encode the
answer. Report a handler that uses `*sql.DB` or makes a business decision as a
correctness problem.

# Correctness

## Errors

An error returned to the caller keeps its chain: wrap it with `%w`, as in
`fmt.Errorf("load user %d: %w", id, err)`. Report wrapping with `%v`, and a failure
recognized by comparing `err.Error()` instead of `errors.Is` or `errors.As`, as a
correctness problem: the caller stops recognizing the failure. Report `panic` on an
expected failure outside `main` the same way.

## Context

A function on a request path takes `ctx context.Context` as its first parameter and
passes it to every call that accepts one. Report `context.Background()` or
`context.TODO()` on such a path, and a `context.Context` stored in a struct, as a
correctness problem: cancellation and deadlines stop there.

## Goroutines

Every started goroutine has a way to stop: a cancelled context, a closed channel or
a `sync.WaitGroup` someone waits on. Report a goroutine with none as a correctness
problem: it leaks with everything it holds.

## Resources

Close `resp.Body` with `defer` after the error check, and check `rows.Err()` after a
`rows.Next()` loop. Report a missing one as a correctness problem. Also report `defer`
inside a loop, which runs only when the function returns.

## Outbound HTTP

`http.Get`, `http.Post` and `http.DefaultClient` have no timeout. Report them on a
production path as a correctness problem: one hung remote blocks the goroutine for
good.

# Complexity

Report a function that this pull request adds or grows to more than 50 lines or more
than 3 levels of nesting as a readability problem, and name the piece to extract.

# Ignore

Vendored and generated code is not a review source.

```review-ignore
vendor/**
**/*_gen.go
**/zz_generated.*.go
```
