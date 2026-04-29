from canopy_core.compat import alias_module

_MODULE = alias_module(__name__, "canopy_core.workflow.entry")


if __name__ == "__main__":
    raise SystemExit(_MODULE.main())
