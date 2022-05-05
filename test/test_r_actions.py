#!/usr/bin/env python
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import os
import shutil

import pytest
from sos import execute_workflow
from sos.targets import file_target
from sos.targets_r import R_library


@pytest.mark.skipif(
    not shutil.which("Rscript") or not shutil.which("pandoc"),
    reason="R or pandoc not installed",
)
def test_rmarkdown(clear_now_and_after):
    """Test action Rmarkdown"""
    if not R_library('rmarkdown').target_exists():
        pytest.xfail('rmarkdown is not properly installed.')

    clear_now_and_after("myreport.html")
    execute_workflow(
        r"""
        [10]

        report:
        ## Some random figure

        Generated by matplotlib


        [100]
        # generate report
        output: 'myreport.html'
        Rmarkdown(output=_output[0])
        """,
        options={"report_output": "report.md"},
    )

    assert os.path.isfile("myreport.html")


@pytest.mark.skipif(
    not shutil.which("Rscript") or not shutil.which("pandoc"),
    reason="R or pandoc not installed",
)
def test_rmarkdown_with_input(clear_now_and_after):
    if not R_library('rmarkdown').target_exists():
        pytest.xfail('rmarkdown is not properly installed.')

    clear_now_and_after("myreport.html")
    # Rmarkdown with specified input.
    execute_workflow(r"""
        [10]
        report: output='a.md'
        ## Some random figure

        Generated by matplotlib


        [100]
        # generate report
        output: 'myreport.html'
        Rmarkdown(input='a.md', output=_output[0])
        """)
    assert os.path.isfile("myreport.html")


@pytest.mark.skipif(
    not shutil.which("Rscript") or not shutil.which("pandoc"),
    reason="R or pandoc not installed",
)
def test_rmarkdown_with_action_output(clear_now_and_after):
    if not R_library('rmarkdown').target_exists():
        pytest.xfail('rmarkdown is not properly installed.')

    clear_now_and_after("default_10.md", "default_20.md", "output.html")
    execute_workflow(
        r"""
        [10]
        report: output='default_10.md'
        A_10

        [20]
        report: output='default_20.md'
        A_20

        [100]
        # generate report
        Rmarkdown(input=['default_10.md', 'default_20.md'], output='output.html')
        """,
        options={"report_output": "${step_name}.md"},
    )
    for f in ["default_10.md", "default_20.md", "output.html"]:
        assert file_target(f).exists()


@pytest.mark.skipif(
    not shutil.which("Rscript") or not shutil.which("pandoc"),
    reason="R or pandoc not installed",
)
def test_rmarkdown_to_stdout():
    if not R_library('rmarkdown').target_exists():
        pytest.xfail('rmarkdown is not properly installed.')
    execute_workflow(r"""
        # generate report
        Rmarkdown:
            # this is title
        """)
