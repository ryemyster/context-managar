#!/usr/bin/env bash

parse_dotenv_line() {
  local line="$1"
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && return 1
  [[ "$line" != *"="* ]] && return 1

  local key="${line%%=*}"
  local val="${line#*=}"

  key="${key#"${key%%[![:space:]]*}"}"
  key="${key%"${key##*[![:space:]]}"}"
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"

  if [[ "$val" != \"* && "$val" != \'* ]]; then
    val="${val%%[[:space:]]#*}"
    val="${val%"${val##*[![:space:]]}"}"
  fi

  if [[ "$val" == \"*\" && "$val" == *\" ]]; then
    val="${val#\"}"
    val="${val%\"}"
  elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
    val="${val#\'}"
    val="${val%\'}"
  fi

  [[ -z "$key" ]] && return 1
  printf '%s=%s\n' "$key" "$val"
}
