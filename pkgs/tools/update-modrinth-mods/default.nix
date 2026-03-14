{
  python3,
  lib,
}:

python3.pkgs.buildPythonApplication {
  pname = "update-modrinth-mods";
  version = "0.1.0";
  format = "other";

  src = ./.;

  dontBuild = true;

  installPhase = ''
    install -Dm755 update-modrinth-mods.py $out/bin/update-modrinth-mods
  '';

  meta = with lib; {
    description = "Resolve Modrinth mods from a TOML manifest into a Nix-compatible lock file";
    mainProgram = "update-modrinth-mods";
  };
}
