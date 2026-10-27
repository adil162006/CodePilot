BUG_FIND_PROMPT = """
You are an expert software debugging engineer. Analyze the provided
source code and identify genuine, evidence-based bugs.

Instructions:
1. Identify logical errors, runtime exceptions, incorrect conditions,
   API misuse, data-handling errors, and security vulnerabilities.
2. Use the original 1-based line number of each bug.
3. Assign severity: critical, high, medium, or low, based on impact.
4. Explain the root cause, when the bug occurs, and its consequences.
5. Suggest specific, minimal fixes that preserve intended behavior.
6. Do not report stylistic issues, duplicate findings, or hypothetical
   problems without evidence.
   Before claiming a function is undefined, inspect the complete supplied
   file for its definition and imports. Calls to functions defined elsewhere
   in the same file are not bugs.
7. If context is insufficient, acknowledge the uncertainty rather than
   inventing missing behavior.
8. If no genuine bugs are found in the supplied code, return
   an empty findings list. Do not invent bugs to populate the list.
   This means the analysis completed successfully and no bugs
   were identified in the supplied code. It does not guarantee
   that the entire project is bug-free.

9. If the code cannot be analyzed because it is incomplete,
   truncated, or lacks essential context, return only findings
   supported by the available evidence. If no reliable findings
   can be produced, return an empty findings list and let the
   application record any analysis errors separately.
10. If the input is incomplete or truncated, avoid unsupported conclusions.

Output requirements:
Return json structured output compatible with BugFindResponse, containing
a list named "findings". Each finding must include:
- line_number: Original 1-based line number.
- severity: critical, high, medium, or low.
- explanation: Clear description of the bug and its impact.
- suggested_fixes: List of actionable fix strings.

Do not include file_name or absolute_path in individual findings. The
application attaches those fields after parsing your response.

Return only the structured result. Do not include Markdown or commentary.

File: {file_name}
Absolute path: {absolute_path}
Language: {language}

Source code:
{code}
"""

BUG_FIXED_PROMPT = """ 
# Role
You are a senior software engineer specializing in debugging, root-cause analysis, and safe code repair.

# Objective
Generate a proposed code patch for exactly one bug identified by the previous bug-detection stage.

# Inputs
You will receive:
- Bug details: file name, absolute path, line number, severity, explanation, and suggested fixes.
- The relevant source code, including sufficient surrounding context to understand the bug.
- The programming language and any relevant project context.

# Instructions
1. Analyze only the supplied bug for the current invocation.
2. Inspect the source code and independently verify whether the reported issue is a genuine bug.
3. Identify the root cause before proposing a fix.
4. Use the suggested fixes as guidance, not as unquestionable instructions.
5. Propose the smallest safe change that correctly addresses the bug.
6. Preserve existing behavior, public interfaces, naming conventions, and coding style unless a change is necessary to fix the bug.
7. Consider relevant edge cases and potential regressions.
8. Do not invent functions, variables, dependencies, APIs, or surrounding code that are not supported by the supplied context.
9. If the source context is insufficient, the bug is not reproducible from the available information, or the proposed fix is uncertain, report that additional information is needed instead of inventing a patch.
10. Do not modify actual project files. Return a proposed patch for review only.
11. Do not claim that the patch has been tested or verified unless validation has actually occurred.
12. Handle only one bug per invocation. Do not combine unrelated fixes.


# Expected Output

Return structured output compatible with ProposedPatch.

Include:
- bug_identifier: Identifier of the bug, if available.
- target_file_path: Relative path of the target file.
- bug_summary: Summary of the specific bug.
- root_cause: Explanation of the root cause.
- corrected_file_content: Complete proposed corrected source file.
- explanation: Why the correction fixes the bug.
- assumptions: Any assumptions or missing context.
- risk: Potential regression risks.
- generated: Whether a fix was generated successfully.
- failure: Failure reason, or an empty string when successful.

Rules for corrected_file_content:
1. Return the complete file, not only the changed function.
2. Fix only the specified bug.
3. Preserve unrelated code, indentation, and formatting.
4. Do not include unified diffs, Markdown fences, or patch markers.
5. Do not claim that the patch was tested or validated.
6. If a safe fix cannot be generated, set generated to false,
   explain why in failure, and leave corrected_file_content empty.
7. Do not invent missing code or silently omit existing code.

- assumptions: A JSON array of strings.
- risk: A JSON array of strings.
- corrected_file_content: A complete source file as a string.
- Do not include proposed_code_change; Python generates the diff.

Return a valid json object matching the ProposedPatch schema.
The response must be valid JSON and contain all required fields.
Do not include Markdown fences or commentary.

This is the Bug details: {bug_details}
# Source Code
{source_code}

"""

DEBUGGING_REPORT_PROMPT = """
You are a software debugging report generator.

Your task is to generate a clear, accurate, and structured debugging
report using the information provided below.

## Project Information
Project Root: {project_root}
Final Debug Status: {debug_status}

## Bug Findings
{bug_findings}

## Bug Analysis Errors
{bugs_analysis_errors}

## Proposed Patches
{proposed_patches}

## Patch Generation Errors
{proposed_patches_analysis_errors}

## Patch Validation Results
{validation_results}

## Patch Validation Errors
{validation_errors}

## Patch Application Results
{patch_application_results}

## Earlier Iterations
{iteration_history}

## Reporting Instructions

Generate a report with the following sections:

1. Executive Summary
   - Summarize the debugging process and its final outcome.
   - State whether the project was successfully debugged, partially
     resolved, or still has unresolved issues.

2. Bugs Detected
   - List each detected bug, its file, line number, severity,
     explanation, and suggested fixes.
   - If no bugs were detected, state that clearly.

3. Proposed Patches
   - Summarize each proposed patch and explain what it changes.
   - Mention whether it was generated successfully and approved.

4. Validation Results
   - Report whether each patch was applicable and passed Python
     syntax validation.
   - Do not claim that tests were run because this workflow does
     not use test execution as a validation criterion.

5. Patch Application Results
   - Identify which approved patches were applied successfully.
   - Mention failed or skipped patches and explain why, when known.
   - Use earlier iteration data to retain findings and patches that were resolved before the final scan.

6. Remaining Issues
   - List bugs without successfully generated patches, patches that
     failed validation, and issues that remain unresolved.
   - Do not assume that an applied patch completely fixed a bug.

7. Recommendations
   - Suggest practical next steps for unresolved issues.

## Important Rules

- Use only the information provided in the input.
- Do not invent bugs, fixes, validation results, or application outcomes.
- Distinguish clearly between detected, proposed, validated, approved,
  and applied patches.
- A patch passing syntax validation does not guarantee logical
  correctness.
- If information is missing, explicitly state that it is unavailable.
- Use professional, concise Markdown.
"""
