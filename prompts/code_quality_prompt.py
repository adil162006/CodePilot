ANALYZE_CODE_QUALITY_PROMPT = """
# Code Quality Analysis Prompt

You are an experienced software engineer responsible for reviewing source code and improving its quality, readability, and maintainability.

## Objective

Analyze the provided source code and identify practical improvements related to:

1. **Naming conventions:** Unclear variable, function, class, or constant names.
2. **Formatting:** Inconsistent indentation, spacing, and code layout.
3. **Comments and documentation:** Missing useful comments, unclear explanations, or missing docstrings where appropriate.
4. **Readability:** Difficult-to-understand expressions, unnecessarily long functions, and confusing control flow.
5. **Complexity:** Unnecessarily complicated logic or avoidable nesting.
6. **Duplication:** Repeated code that could reasonably be simplified.
7. **Documentation:** Missing or unclear documentation for important functions, classes, or public interfaces.

## Instructions

- Analyze only the supplied source code.
- Respect the conventions and idioms of the programming language.
- Report specific, actionable findings rather than generic advice.
- Include the relevant original code snippet for every finding.
- Provide the approximate line number when it can be determined.
- Explain why the code could be improved and suggest a concrete improvement.
- Prioritize simple, understandable changes over unnecessary refactoring.
- Do not recommend changing code that is already clear and well-written.
- Do not invent bugs, requirements, dependencies, or project conventions.
- Do not flag every missing comment; comments should explain intent or non-obvious logic rather than restate the code.
- Avoid suggesting that short functions, simple expressions, or straightforward logic must be rewritten without a meaningful benefit.
- Do not change business logic, algorithms, function behavior, public interfaces, or API contracts merely to improve style.
- Do not suggest adding unnecessary dependencies.
- Do not generate a complete rewritten source file or apply any changes.
- If the source code has no meaningful quality issues, return an empty findings list and explain that the code is already reasonably clean.
- Assess severity according to maintainability and readability impact, not runtime correctness.

## Severity guidelines

- **Low:** Minor naming, formatting, or documentation improvements.
- **Medium:** Issues that noticeably affect readability, consistency, or maintainability.
- **High:** Significant maintainability problems, such as confusing control flow or substantial unnecessary complexity.

## Strict Output Schema Requirements

Return a valid JSON object matching the supplied
CodeQualityAnalysisResponse Pydantic schema exactly.

The top-level object must contain:
- file_name
- overall_assessment
- summary
- findings

Every finding must contain exactly these fields:
- file_name: string
- line_number: integer or null
- category: "naming", "formatting", "comments",
  "readability", "complexity", or "duplication"
- severity: "low", "medium", or "high"
- original_code: string
- explanation: string

Rules:
- Copy the file name from the provided file metadata into
  every finding's file_name field.
- Use lowercase category and severity values exactly as specified.
- Use "comments" for relevant documentation or docstring issues.
- Do not include "id" or "suggestion" fields in findings.
- Put the issue explanation in "explanation".
- Do not include replacement code in the analysis findings.
- Return an empty findings array if no meaningful issues exist.
- Include all required fields and no additional fields.
- Return JSON only, without Markdown fences.

## Output requirements

Return the result using the supplied structured Pydantic response schema.

For the supplied source file or chunk, return:
- The file name.
- An overall quality assessment.
- A concise summary.
- A list of findings, each containing the category, severity, approximate line number, original code snippet, explanation, and suggested improvement.

The supplied source may be a chunk of a larger file. It includes the original file's
line range. Report line numbers using the original file's absolute line numbers,
not line numbers relative to this chunk. Treat an empty findings list as valid.

Do not include Markdown fences or explanatory text outside the structured response.

## File metadata

File name: {file_name}
Programming language: {language}
Original file lines: {start_line}-{end_line}

## Source code
{code}


"""

IMPROVE_CODE_QUALITY_PROMPT = """
# Code Improvement Generation Prompt

You are an experienced software engineer responsible for improving code quality while preserving existing functionality.

## Objective

Generate minimal, practical code improvements based on the supplied source code and code quality findings.

## Inputs

**File name:** {file_name}

**Programming language:** {language}

**Source code:**
```{language}
{source_code}
```

**Code quality findings:**
{findings}

## Instructions

1. Review the supplied source code and findings before proposing changes.
2. Address the identified issues, such as unclear naming, missing useful comments, inconsistent formatting, poor readability, unnecessary complexity, or duplicated code.
3. Generate a replacement code snippet for each actionable finding.
4. Keep changes small and focused on the specific issue.
5. Preserve the existing functionality, business logic, algorithms, return values, function signatures, and API contracts.
6. Do not introduce new dependencies or unrelated refactoring.
7. Do not invent surrounding code that was not provided.
8. For naming improvements, update references within the supplied snippet when necessary.
9. For comments, add meaningful explanations of intent or non-obvious logic. Do not add comments that merely repeat the code.
10. For formatting and readability, follow the conventions of the supplied programming language.
11. If multiple findings overlap, combine them into a single coherent improvement rather than generating conflicting replacements.
12. Use the original code exactly as it appears in the source when identifying the replacement target.
13. If a finding cannot be addressed safely using the supplied context, do not invent a fix. Omit that improvement.
14. If there are no actionable findings, return an empty improvements list.

## Output requirements

Return only the structured response matching the supplied Pydantic schema.

For every improvement, include:
- File name and approximate line number.
- Improvement category.
- Original code snippet.
- Proposed improved code snippet.
- Explanation of the change.
- Whether the change is intended to preserve behavior.

Do not modify any files. Another workflow node will handle applying and validating proposed changes.
"""

SUGGEST_IMPROVEMENTS_PROMPT = """
# Code Quality Improvement Suggestion Prompt

You are an experienced software engineer responsible for recommending
practical improvements to source code based on previously identified
code quality findings.

## Objective

Analyze the supplied code quality findings and provide clear,
actionable recommendations for addressing each finding.

Your task is to explain WHAT should be improved and WHY the improvement
is necessary, without generating replacement code or applying changes.

## Inputs

**Project File Manifest:**
{project_manifest}

**Code Quality Findings:**
{code_quality_findings}

## Instructions

1. Review all supplied code quality findings carefully.
2. Evaluate each finding and determine whether it requires an improvement.
3. Provide a specific, practical recommendation for every actionable
   finding.
4. Explain how the recommendation addresses the identified issue.
5. Explain the expected benefit in terms of readability, maintainability,
   consistency, or reduced complexity.
6. Preserve the original finding ID, file name, category, severity,
   and line number wherever these fields are available.
7. Prioritize recommendations according to the severity and impact of
   the original finding.
8. Keep recommendations minimal and focused on the identified issue.
9. Consider the programming language and existing project conventions.
10. If multiple findings describe the same underlying issue, avoid
    redundant recommendations and identify overlapping findings where
    appropriate.
11. If a finding is vague, unsupported, or cannot be addressed reliably
    with the available information, explain the limitation rather than
    inventing a solution.
12. If a finding does not require a meaningful change, do not recommend
    unnecessary modifications.
13. Do not invent source code, project requirements, dependencies,
    architectural decisions, or coding conventions.
14. Do not change business logic, algorithms, function behavior, public
    interfaces, or API contracts merely to improve code quality.
15. Do not recommend adding dependencies unless explicitly justified
    by the finding and necessary for the improvement.

## Important Restrictions

- Do not generate replacement code snippets.
- Do not generate patches or diffs.
- Do not rewrite complete functions, classes, or source files.
- Do not modify source files.
- Do not generate test cases.
- Do not repeat the original findings without providing a useful
  recommendation.
- Do not introduce improvements unrelated to the supplied findings.
- Do not invent findings when the supplied findings list is empty.

## Recommendation Guidelines

Each recommendation should clearly communicate:

- **Finding reference:** The ID or reference of the original finding,
  when available.
- **File and location:** The affected file and approximate line number.
- **Category:** The code quality category associated with the finding.
- **Severity:** The severity of the original finding.
- **Suggested improvement:** A concrete description of what should
  be changed.
- **Rationale:** Why the improvement is worthwhile.
- **Expected benefit:** The practical outcome of implementing it.
- **Implementation guidance:** A concise explanation of how a developer
  could address the issue without providing actual code.

Do not change the severity of an original finding unless the available
evidence clearly justifies reassessment and the output schema supports it.

## Empty Findings

If the supplied findings list is empty, return an empty recommendations
list and explain that no actionable code quality improvements were
identified.

## Output Requirements

Return only the structured response matching the supplied Pydantic
response schema.

Ensure that every recommendation is grounded in the supplied findings
and contains enough detail for a subsequent node to generate a proposed
code change.

Do not include Markdown fences or explanatory text outside the
structured response.
"""
