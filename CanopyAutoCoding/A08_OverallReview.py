from canopy_core.compat import alias_module

_MODULE = alias_module(__name__, "canopy_core.stage_kernel.overall_review")


if __name__ == "__main__":
    raise SystemExit(_MODULE.main())
