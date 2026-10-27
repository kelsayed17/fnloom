# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/kelsayed17/fnloom/security/advisories/new). Do not open a public issue.

Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement within a
week, and a fix or a plan within 30 days for a confirmed issue.

## Supported versions

fnloom has not been released. Until it is, only `main` receives fixes.

## Scope

fnloom runs the functions you give it; running them is not a vulnerability. These are in scope:

- memory unsafety reachable from Python: a crash, a use-after-free or a corrupted reference count caused by the native
  extension `fnloom._core`, through any public API or garbage collection;
- a Rust panic that crosses into Python as anything but a Python exception, or that aborts the interpreter;
- a value from one run, or from one plan, reaching another run or a node that did not ask for it;
- a crafted graph (deep, wide or cyclic) that makes building, planning or running use time or memory far beyond its
  size.
