{ pkgs, lib, config, ... }:

{
  env.PYTHONUNBUFFERED = "true";
  env.MYPYPATH="stubs";
  env.DYLD_LIBRARY_PATH = "${pkgs.fswatch}/lib";

  packages = [
    pkgs.git
    pkgs.fswatch
  ];

  languages.python = {
    enable  = true;
    package = pkgs.python311Full;
    poetry = {
      enable = true;
      install = {
         enable = true;
         installRootPackage = true;
      };
    };
  };

  process-managers.overmind.enable = true;
  process-managers.honcho.enable = false;

  processes = {
    # aramaki-agent.exec = "aramaki-agent";
  };

  pre-commit.hooks = {
    shellcheck.enable = true;
    black.enable = true;
    isort.enable = true;
    ruff.enable = true;
  };

}
