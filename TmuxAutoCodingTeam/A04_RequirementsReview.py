from tmux_core.compat import alias_module

_MODULE = alias_module(__name__, "tmux_core.stage_kernel.requirements_review")


if __name__ == "__main__":
    raise SystemExit(_MODULE.main())
