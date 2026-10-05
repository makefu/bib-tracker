{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.bib-tracker;

  enabledAccounts = lib.filterAttrs (_: a: a.enable) cfg.accounts;

  credentialName = name: "account-${name}-password";

  # The unit's configuration is two merged YAML files rather than a pile of
  # environment variables: one that may be world-readable, and one holding
  # the account credentials. A secret is never inline; an account names a
  # systemd credential, which the unit exposes under $CREDENTIALS_DIRECTORY
  # at runtime, so nothing here can carry a password into the store.
  yamlFormat = pkgs.formats.yaml { };

  # Everything except the accounts, which go in the secrets file: an account
  # names its credential there, and merging happens per account name, so the
  # public file may stay public while the names stay where the secret is.
  publicConfigYaml = yamlFormat.generate "bib-tracker-config.yaml" (
    {
      db_path = toString cfg.dbPath;
      host = cfg.listenAddress;
      port = cfg.port;
      log_level = cfg.logLevel;
      poll_interval_minutes = cfg.poll.intervalMinutes;
      poll_jitter_seconds = cfg.poll.jitterSeconds;
      poll_max_concurrent = cfg.poll.maxConcurrent;
      poll_on_startup = cfg.poll.onStartup;
      zero_result_confirmations = cfg.poll.zeroResultConfirmations;
      renew_threshold_days = cfg.renewThresholdDays;
      metadata_enabled = cfg.metadata.enable;
      metadata_providers = cfg.metadata.providers;
      price_providers = cfg.metadata.priceProviders;
      metadata_base_urls = cfg.metadata.baseUrls;
      metadata_rate_limits = cfg.metadata.rateLimits;
      # Credential names, resolved against $CREDENTIALS_DIRECTORY at runtime.
      metadata_api_key_files = lib.mapAttrs' (name: _: lib.nameValuePair name "provider-${name}-key") cfg.metadata.apiKeyFiles;
      user_agent_contact = cfg.metadata.userAgentContact;
      default_prices = cfg.defaultPrices;
    }
    // cfg.extraConfig
  );

  accountsYaml = lib.mapAttrs (name: a: {
    name = name;
    library_type = a.libraryType;
    username = a.username;
    base_url = a.baseUrl;
    display_name = a.displayName;
    colour = a.colour;
    enabled = true;
    password_credential = credentialName name;
    loan_period_days = a.loanPeriodDays;
  }) enabledAccounts;

  # The generated file carries no secret text, only credential names, but
  # the split mirrors the standalone layout: open config plus secrets file.
  secretsConfigYaml = yamlFormat.generate "bib-tracker-secrets.yaml" {
    accounts = accountsYaml;
  };
in
{
  options.services.bib-tracker = {
    enable = lib.mkEnableOption "bib-tracker, a library lending history tracker";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.bib-tracker";
      description = "The bib-tracker package to run.";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address to bind to. There is no authentication, so put a reverse proxy in front before widening this.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8099;
      description = "TCP port to listen on.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open {option}`services.bib-tracker.port` in the firewall.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "bib-tracker";
      description = "Name below /var/lib holding the database.";
    };

    dbPath = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/${cfg.stateDir}/bib-tracker.db";
      defaultText = lib.literalExpression ''"/var/lib/''${stateDir}/bib-tracker.db"'';
      description = "Path to the SQLite database. Holds all history, metadata and cover images.";
    };

    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "User to run as. Null uses a systemd DynamicUser.";
    };

    group = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Group to run as. Null uses a systemd DynamicUser.";
    };

    logLevel = lib.mkOption {
      type = lib.types.enum [
        "debug"
        "info"
        "warning"
        "error"
      ];
      default = "info";
      description = "Log verbosity.";
    };

    poll = {
      intervalMinutes = lib.mkOption {
        type = lib.types.ints.positive;
        default = 360;
        description = "Minutes between polls of each account. Lend and return dates are day-granular, so polling more often than a few times a day buys no accuracy and only loads the library servers.";
      };

      jitterSeconds = lib.mkOption {
        type = lib.types.ints.unsigned;
        default = 300;
        description = "Random spread applied to each poll, so accounts do not all fire at once.";
      };

      maxConcurrent = lib.mkOption {
        type = lib.types.ints.positive;
        default = 2;
        description = "How many accounts may be polled at the same time.";
      };

      onStartup = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Poll once when the service starts.";
      };

      zeroResultConfirmations = lib.mkOption {
        type = lib.types.ints.positive;
        default = 2;
        description = "How many consecutive empty results are required before believing an account really was emptied. Guards the history against a silent scraper breakage, at the cost of one poll interval of latency when everything genuinely was returned.";
      };
    };

    renewThresholdDays = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 3;
      description = "\"Renew all due\" acts on loans due within this many days.";
    };

    accounts = lib.mkOption {
      default = { };
      description = "Library accounts to track, keyed by a short name.";
      example = lib.literalExpression ''
        {
          stuttgart = {
            libraryType = "stuttgart";
            username = "123456";
            passwordFile = "/run/secrets/bib-stuttgart";
          };
        }
      '';
      type = lib.types.attrsOf (
        lib.types.submodule {
          options = {
            enable = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Whether to poll this account.";
            };

            libraryType = lib.mkOption {
              type = lib.types.enum [
                "remseck"
                "stuttgart"
              ];
              description = "Which library backend to use.";
            };

            username = lib.mkOption {
              type = lib.types.str;
              description = "Library card number.";
            };

            passwordFile = lib.mkOption {
              type = lib.types.path;
              description = "File holding the account password. Read at start-up through systemd's credential store, so it never enters the Nix store.";
            };

            baseUrl = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Override the OPAC base URL. Only needed for another installation of the same software, or for testing.";
            };

            displayName = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Name shown in the interface.";
            };

            colour = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "CSS colour used to tag this account's rows.";
            };

            loanPeriodDays = lib.mkOption {
              type = lib.types.attrsOf lib.types.ints.positive;
              default = { };
              example = {
                book = 28;
                game = 14;
              };
              description = "Standard loan period per media class. Used to estimate a lend date from a due date when the OPAC does not report one.";
            };
          };
        }
      );
    };

    metadata = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Look up covers, descriptions, ratings and list prices from external APIs.";
      };

      providers = lib.mkOption {
        type = lib.types.listOf (
          lib.types.enum [
            "openlibrary"
            "googlebooks"
            "dnb"
            "bgg"
            "wikidata"
            "vlb"
          ]
        );
        default = [
          "openlibrary"
          "dnb"
          "wikidata"
        ];
        description = ''
          Metadata providers to query.

          Open Library, the DNB and Wikidata need no credentials. Google Books
          works anonymously only until its per-address daily quota runs out, and
          BoardGameGeek refuses anonymous requests outright, so both are left out
          by default: add them together with an entry in
          {option}`services.bib-tracker.metadata.apiKeyFiles`.

          Wikidata is what board games get without a BoardGameGeek credential.
          It has publisher, year and player counts, but no community rating.
        '';
      };

      priceProviders = lib.mkOption {
        type = lib.types.listOf (
          lib.types.enum [
            "vlb"
            "dnb"
            "googlebooks"
            "openlibrary"
          ]
        );
        default = [
          "vlb"
          "dnb"
          "googlebooks"
        ];
        description = ''
          Which providers may supply a purchase price, most trusted first.

          The VLB is, under the Börsenverein's Verkehrsordnung, the reference
          database for the gebundener Ladenpreis, and a price recorded there
          takes precedence over one on a publisher's own site. It needs a
          contract with MVB, so without one the ladder falls through.

          The DNB carries the price as catalogued in MARC 020 $c, free and with
          good coverage of German titles. Later price changes or a lifted price
          binding are not reflected there, which is fine for "what would this
          have cost" and not a claim about today's price.

          Google Books is the fallback and rarely knows German titles at all.

          A price entered by hand always beats every entry in this list.
        '';
      };

      apiKeyFiles = lib.mkOption {
        type = lib.types.attrsOf lib.types.path;
        default = { };
        example = lib.literalExpression ''
          {
            googlebooks = "/run/secrets/google-books-key";
            bgg = "/run/secrets/bgg-token";
            vlb = "/run/secrets/vlb-token";
          }
        '';
        description = "Credential files per provider, read through systemd's credential store so they never enter the Nix store.";
      };

      rateLimits = lib.mkOption {
        type = lib.types.attrsOf lib.types.ints.positive;
        default = { };
        example = {
          openlibrary = 60;
          dnb = 30;
        };
        description = "Requests per minute per provider. These are free services run by libraries and volunteers; the defaults are deliberately gentle.";
      };

      userAgentContact = lib.mkOption {
        type = lib.types.str;
        default = "https://github.com/makefu/bib-tracker";
        description = "Contact URL sent in the User-Agent. Open Library and the DNB ask for one.";
      };

      baseUrls = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        description = "Override a provider's base URL. Used by the VM test to keep it offline.";
      };
    };

    defaultPrices = lib.mkOption {
      type = lib.types.attrsOf lib.types.numbers.nonnegative;
      default = {
        book = 15.0;
        audiobook = 12.0;
        music = 10.0;
        movie = 10.0;
        game = 35.0;
        magazine = 5.0;
        other = 10.0;
      };
      description = "Assumed purchase price in EUR per media class, used for \"money saved\" when no list price could be found. Figures derived from these are always labelled as estimates.";
    };

    extraConfig = lib.mkOption {
      type = lib.types.attrsOf lib.types.raw;
      default = { };
      description = "Extra keys for the generated config file, for settings without a dedicated option.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.user == null -> lib.hasPrefix "/var/lib/${cfg.stateDir}/" (toString cfg.dbPath);
        message = "services.bib-tracker.dbPath must live under /var/lib/${cfg.stateDir} when running as a DynamicUser. Set services.bib-tracker.user and .group to use another location.";
      }
      {
        assertion = (cfg.user == null) == (cfg.group == null);
        message = "services.bib-tracker: set both user and group, or neither.";
      }
    ];

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.bib-tracker = {
      description = "bib-tracker library lending history";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      # The application merges these two YAML files; the unit no longer
      # needs a BIB_TRACKER_* variable for anything.
      environment.BIB_TRACKER_CONFIG_FILES = "${publicConfigYaml}:${secretsConfigYaml}";

      serviceConfig = {
        Type = "exec";
        ExecStartPre = "${lib.getExe' cfg.package "bib-tracker-migrate"}";
        ExecStart = lib.getExe cfg.package;
        Restart = "on-failure";
        RestartSec = "10s";
        WorkingDirectory = "/var/lib/${cfg.stateDir}";

        StateDirectory = cfg.stateDir;
        StateDirectoryMode = "0700";

        # Passwords reach the process through a per-unit tmpfs that only this
        # unit can read, so nothing is written to persistent storage and it
        # works with DynamicUser, whose uid is not known at evaluation time.
        LoadCredential =
          lib.mapAttrsToList (name: a: "${credentialName name}:${toString a.passwordFile}") enabledAccounts
          ++ lib.mapAttrsToList (
            name: file: "provider-${name}-key:${toString file}"
          ) cfg.metadata.apiKeyFiles;
      }
      // (
        if cfg.user == null then
          { DynamicUser = true; }
        else
          {
            User = cfg.user;
            Group = cfg.group;
          }
      )
      // {
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectProc = "invisible";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        LockPersonality = true;
        CapabilityBoundingSet = [ "" ];
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" ];
        UMask = "0077";
        # Deliberately no MemoryDenyWriteExecute: CPython needs W^X-violating
        # mappings and the service will not start with it enabled.
      };
    };
  };
}
