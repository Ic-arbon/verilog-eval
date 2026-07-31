#=========================================================================
# Toplevel configure.ac for running LLM codegen experiments
#=========================================================================
# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
# Author : Christopher Batten, NVIDIA and Nathaniel Pinckney, NVIDIA

#-------------------------------------------------------------------------
# Project metadata
#-------------------------------------------------------------------------

m4_define( proj_name,         [LLM Code Generation Experiments])
m4_define( proj_maintainer,   [Christopher Batten])
m4_define( proj_abbreviation, [verilog-eval-v2])

#-------------------------------------------------------------------------
# Project version information
#-------------------------------------------------------------------------
# Version information is meant to be managed through a version control
# system's tags and revision numbers. In a working copy the version will
# not be defined here (you should just use the version control system's
# mechanisms). When we make a distribution then we can set the version
# here as formed by the scripts/vcs-version.sh script so that the
# distribution knows what version it came from. If you are not using
# version control then it is fine to set this directly.

m4_define( proj_version, [?])

#-------------------------------------------------------------------------
# Setup
#-------------------------------------------------------------------------

AC_INIT(proj_name,proj_version,proj_maintainer,proj_abbreviation)
AC_CONFIG_SRCDIR([scripts/sv-generate])

#-------------------------------------------------------------------------
# Basic checks
#-------------------------------------------------------------------------

AC_CHECK_PROGS([IVERILOG],[iverilog],[no])
AS_IF([test "${IVERILOG}" = "no"],
[
  AC_MSG_ERROR([Must have iverilog installed!])
])

#-------------------------------------------------------------------------
# --with-model
#-------------------------------------------------------------------------

AC_ARG_WITH(model,
  AS_HELP_STRING([--with-model],[Large language model to use]),
  [model="${with_model}"],
  [model="gpt4-turbo"])

AC_SUBST([model])
AC_MSG_NOTICE([using model: ${model}])

#-------------------------------------------------------------------------
# Generator Program Protocol selection
#-------------------------------------------------------------------------

AC_ARG_WITH(generator,
  AS_HELP_STRING([--with-generator],[Sample producer: model or agent]),
  [generator="${with_generator}"],
  [generator="model"])

AS_CASE([${generator}],
  [model|agent], [],
  [AC_MSG_ERROR([--with-generator must be model or agent])])

AC_SUBST([generator])
AC_MSG_NOTICE([using generator: ${generator}])

AC_ARG_WITH(agent,
  AS_HELP_STRING([--with-agent],[External Agent: pi or opencode]),
  [agent="${with_agent}"],
  [agent="opencode"])

AS_CASE([${agent}],
  [pi|opencode], [],
  [AC_MSG_ERROR([--with-agent must be pi or opencode])])

AC_SUBST([agent])
AC_MSG_NOTICE([using Agent: ${agent}])

AC_ARG_WITH(agent-timeout,
  AS_HELP_STRING([--with-agent-timeout],[Agent wall timeout in seconds]),
  [agent_timeout="${with_agent_timeout}"],
  [agent_timeout=300])
AC_SUBST([agent_timeout])

AC_ARG_WITH(agent-max-turns,
  AS_HELP_STRING([--with-agent-max-turns],[Maximum Agent turns]),
  [agent_max_turns="${with_agent_max_turns}"],
  [agent_max_turns=20])
AC_SUBST([agent_max_turns])

AC_ARG_WITH(agent-max-tool-calls,
  AS_HELP_STRING([--with-agent-max-tool-calls],[Maximum Agent tool calls]),
  [agent_max_tool_calls="${with_agent_max_tool_calls}"],
  [agent_max_tool_calls=50])
AC_SUBST([agent_max_tool_calls])

AC_ARG_WITH(agent-max-input-tokens,
  AS_HELP_STRING([--with-agent-max-input-tokens],[Maximum Agent input context tokens]),
  [agent_max_input_tokens="${with_agent_max_input_tokens}"],
  [agent_max_input_tokens=16384])
AC_SUBST([agent_max_input_tokens])

AS_IF([test "${agent_timeout}" -ge 1 2>/dev/null], [],
  [AC_MSG_ERROR([Agent budgets must be positive integers])])
AS_IF([test "${agent_max_turns}" -ge 1 2>/dev/null], [],
  [AC_MSG_ERROR([Agent budgets must be positive integers])])
AS_IF([test "${agent_max_tool_calls}" -ge 1 2>/dev/null], [],
  [AC_MSG_ERROR([Agent budgets must be positive integers])])
AS_IF([test "${agent_max_input_tokens}" -ge 1 2>/dev/null], [],
  [AC_MSG_ERROR([Agent budgets must be positive integers])])

AC_ARG_WITH(agent-thinking,
  AS_HELP_STRING([--with-agent-thinking],[Enable request-level Agent thinking: on or off]),
  [agent_thinking="${with_agent_thinking}"],
  [agent_thinking=on])

AS_CASE([${agent_thinking}],
  [on|off], [],
  [AC_MSG_ERROR([--with-agent-thinking must be on or off])])
AC_SUBST([agent_thinking])

AC_ARG_WITH(agent-tool-profile,
  AS_HELP_STRING([--with-agent-tool-profile],[Agent tool profile: base or rtl]),
  [agent_tool_profile="${with_agent_tool_profile}"],
  [agent_tool_profile=base])

AS_CASE([${agent_tool_profile}],
  [base|rtl], [],
  [AC_MSG_ERROR([--with-agent-tool-profile must be base or rtl])])
AC_SUBST([agent_tool_profile])

AC_MSG_NOTICE([using Agent timeout: ${agent_timeout}])
AC_MSG_NOTICE([using Agent max turns: ${agent_max_turns}])
AC_MSG_NOTICE([using Agent max tool calls: ${agent_max_tool_calls}])
AC_MSG_NOTICE([using Agent max input tokens: ${agent_max_input_tokens}])
AC_MSG_NOTICE([using Agent thinking: ${agent_thinking}])
AC_MSG_NOTICE([using Agent tool profile: ${agent_tool_profile}])

#-------------------------------------------------------------------------
# --with-examples
#-------------------------------------------------------------------------

AC_ARG_WITH(examples,
  AS_HELP_STRING([--with-examples],[Include in-context learning examples with settable number of shots]),
  [examples="${with_examples}"],
  [examples="0"])

AC_SUBST([examples])
AC_MSG_NOTICE([using in-context learning examples with shots: ${examples}])

#-------------------------------------------------------------------------
# --with-rules
#-------------------------------------------------------------------------

AC_ARG_WITH(rules,
  AS_HELP_STRING([--with-rules],[Include in-context learning rules]),
  [rules="yes"],
  [rules="no"])

AC_SUBST([rules])
AC_MSG_NOTICE([using in-context learning rules: ${rules}])

#-------------------------------------------------------------------------
# --with-task
#-------------------------------------------------------------------------

AC_ARG_WITH(task,
  AS_HELP_STRING([--with-task],[Task to execute]),
  [task="${with_task}"],
  [task="spec-to-rtl"])

AC_SUBST([task])
AC_MSG_NOTICE([using task: ${task}])

#-------------------------------------------------------------------------
# --with-samples
#-------------------------------------------------------------------------

AC_ARG_WITH(samples,
  AS_HELP_STRING([--with-samples],[Number of samples for each problem]),
  [samples="${with_samples}"],
  [samples="20"])

AC_SUBST([samples])
AC_MSG_NOTICE([using samples: ${samples}])

#-------------------------------------------------------------------------
# --with-max-tokens
#-------------------------------------------------------------------------

AC_ARG_WITH(max-tokens,
  AS_HELP_STRING([--with-max-tokens],[Maximum response tokens per model query]),
  [max_tokens="${with_max_tokens}"],
  [max_tokens=1024])

AC_SUBST([max_tokens])
AC_MSG_NOTICE([using max response tokens: ${max_tokens}])

#-------------------------------------------------------------------------
# --with-temperature
#-------------------------------------------------------------------------

AC_ARG_WITH(temperature,
  AS_HELP_STRING([--with-temperature],[Temperature of model]),
  [temperature="${with_temperature}"],
  [temperature=0.85])

AC_SUBST([temperature])
AC_MSG_NOTICE([using temperature: ${temperature}])

#-------------------------------------------------------------------------
# --with-top-p 
#-------------------------------------------------------------------------

AC_ARG_WITH(top-p,
  AS_HELP_STRING([--with-top-p],[Top-p of model]),
  [top_p="${with_top_p}"],
  [top_p=0.95])

AC_SUBST([top_p])
AC_MSG_NOTICE([using top-p: ${top_p}])


#-------------------------------------------------------------------------
# dataset
#-------------------------------------------------------------------------

AC_ARG_WITH(dataset,
  AS_HELP_STRING([--with-dataset],[Directory with dataset files]),
  [dataset_dir="${with_dataset}"],
  [dataset_dir="${srcdir}/dataset_${task}"])

AC_SUBST([dataset_dir])
AC_MSG_NOTICE([using dataset: ${dataset_dir}])

#-------------------------------------------------------------------------
# problems
#-------------------------------------------------------------------------

AC_ARG_WITH(problems,
  AS_HELP_STRING([--with-problems],[File with list of problems]),
  [problems_file="${with_problems}"],
  [problems_file="${dataset_dir}/problems.txt"])

AC_SUBST([problems_file])
AC_MSG_NOTICE([using problems: ${problems_file}])

#-------------------------------------------------------------------------
# create prompt.mk file
#-------------------------------------------------------------------------

AC_MSG_NOTICE([creating problems.mk])

cat > problems.mk \
<<'END'
#=========================================================================
# problems.mk
#=========================================================================
# Generated by the configure script from dataset/problems.txt

problems = \
END

sed -e '/^\s*$/d' -e 's/^\(.*\)$/  \1 \\/' \
    ${problems_file} >> problems.mk

echo "" >> problems.mk

#-------------------------------------------------------------------------
# create samples.mk file
#-------------------------------------------------------------------------

AC_MSG_NOTICE([creating samples.mk])

cat > samples.mk \
<<'END'
#=========================================================================
# samples.mk
#=========================================================================
# Generated by the configure script from dataset/problems.txt

END

sed -e '/^\s*$/d' -e "s/^\(.*\)$/\1_num_samples =  ${samples}/" \
    ${problems_file} | column -t >> samples.mk

echo "" >> samples.mk

#-------------------------------------------------------------------------
# pregen
#-------------------------------------------------------------------------

AC_ARG_WITH(pregen-dir,
  AS_HELP_STRING([--with-pregen-dir],[Directory for storing pregen files]),
  [pregen_dir="${with_pregen_dir}"],
  [pregen_dir="DEFAULT"])

AS_IF([ test "${pregen_dir}" != "DEFAULT" ],
[
  AC_MSG_NOTICE([using pregen dir: ${pregen_dir}])
],[
  if [[ -d "../../verilog-eval-v2-pregen" ]]; then
    pregen_dir="../../verilog-eval-v2-pregen"
    AC_MSG_NOTICE([using pregen dir: ${pregen_dir}])
  else
    AC_MSG_NOTICE([no pregen dir specified])
  fi
])

AC_SUBST([pregen_dir])

AC_ARG_WITH(pregen,
  AS_HELP_STRING([--with-pregen],[Use pregen files]),
  [pregen_en="yes"],
  [pregen_en="no"])

AS_IF([ test "${pregen_en}" = "yes" ],
[
  AC_MSG_NOTICE([using pregen: ${with_pregen}])
  cp -r ${with_pregen}/* .
])

#-------------------------------------------------------------------------
# Output
#-------------------------------------------------------------------------

AC_CONFIG_FILES([Makefile])
AC_OUTPUT
