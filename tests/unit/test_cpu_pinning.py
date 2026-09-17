# SPDX-FileCopyrightText: 2024 Canonical Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Concise unit tests for epa_orchestrator.cpu_pinning."""

from unittest.mock import mock_open, patch

import pytest

from epa_orchestrator.cpu_pinning import calculate_cpu_pinning, get_isolated_cpus


class TestCpuPinning:
    """Unit tests for CPU pinning logic."""

    def test_calculate_cpu_pinning_basic(self, mock_logging):
        """Test basic CPU pinning calculation."""
        shared, dedicated = calculate_cpu_pinning("0-3", 2)
        assert shared == "2-3"
        assert dedicated == "0-1"

    def test_calculate_cpu_pinning_too_many(self, mock_logging):
        """Test requesting too many cores."""
        shared, dedicated = calculate_cpu_pinning("0-3", 5)
        assert shared == ""
        assert dedicated == ""
        mock_logging.error.assert_called()

    def test_calculate_cpu_pinning_zero_request_small_system(self, mock_logging):
        """Test zero cores requested on small system."""
        shared, dedicated = calculate_cpu_pinning("0-9", 0)  # 10 cores total
        assert shared == "8-9"  # 20% = 2 cores
        assert dedicated == "0-7"  # 80% = 8 cores
        mock_logging.info.assert_called_with(
            "Small system detected (10 cores ≤ 100 threshold). "
            "Allocating 8 cores (80% of 10 total CPUs)"
        )

    def test_calculate_cpu_pinning_zero_request_large_system(self, mock_logging):
        """Test zero cores requested on large system (>100 cores) - reserves 16 cores."""
        shared, dedicated = calculate_cpu_pinning("0-149", 0)  # 150 cores total
        assert shared == "134-149"  # 16 reserved cores
        assert dedicated == "0-133"  # 134 allocated cores (150 - 16)
        mock_logging.info.assert_called_with(
            "Large system detected (150 cores > 100 threshold). "
            "Allocating 134 cores (reserving 16 cores)"
        )

    def test_calculate_cpu_pinning_zero_request_threshold_edge_case(self, mock_logging):
        """Test zero cores requested at threshold edge case."""
        shared, dedicated = calculate_cpu_pinning("0-99", 0)  # 100 cores total
        assert shared == "80-99"  # 20% = 20 cores
        assert dedicated == "0-79"  # 80% = 80 cores
        mock_logging.info.assert_called_with(
            "Small system detected (100 cores ≤ 100 threshold). "
            "Allocating 80 cores (80% of 100 total CPUs)"
        )

    def test_calculate_cpu_pinning_zero_request_just_above_threshold(self, mock_logging):
        """Test zero cores requested just above threshold."""
        shared, dedicated = calculate_cpu_pinning("0-100", 0)  # 101 cores total
        assert shared == "85-100"  # 16 reserved cores
        assert dedicated == "0-84"  # 85 allocated cores (101 - 16)
        mock_logging.info.assert_called_with(
            "Large system detected (101 cores > 100 threshold). "
            "Allocating 85 cores (reserving 16 cores)"
        )

    def test_calculate_cpu_pinning_zero_request_very_large_system(self, mock_logging):
        """Test zero cores requested on very large system."""
        shared, dedicated = calculate_cpu_pinning("0-199", 0)  # 200 cores total
        assert shared == "184-199"  # 16 reserved cores
        assert dedicated == "0-183"  # 184 allocated cores (200 - 16)
        mock_logging.info.assert_called_with(
            "Large system detected (200 cores > 100 threshold). "
            "Allocating 184 cores (reserving 16 cores)"
        )

    def test_calculate_cpu_pinning_zero_request_system_smaller_than_reserved(self, mock_logging):
        """Test zero cores requested on system smaller than reserved cores."""
        shared, dedicated = calculate_cpu_pinning("0-9", 0)  # 10 cores, but 15 reserved
        # This should still work because it's a small system (≤25), so uses 80% allocation
        assert shared == "8-9"  # 20% = 2 cores
        assert dedicated == "0-7"  # 80% = 8 cores

    def test_calculate_cpu_pinning_negative_request(self, mock_logging):
        """Test negative core request."""
        shared, dedicated = calculate_cpu_pinning("0-9", -1)  # 10 cores total
        assert shared == "8-9"  # 20% = 2 cores
        assert dedicated == "0-7"  # 80% = 8 cores (treated as 0)
        mock_logging.warning.assert_called_with("Negative cores_requested (-1), treating as 0")

    def test_get_isolated_cpus_success(self, mock_cpu_files, mock_logging):
        """Test successful retrieval of isolated CPUs."""
        result = get_isolated_cpus()
        assert result == "0-3,6-7"
        mock_logging.info.assert_called()

    def test_get_isolated_cpus_no_isolated(self, mock_logging):
        """Test behavior when no isolated CPUs are configured."""
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_isolated_cpus()
            assert result == ""
            # No logging should happen for empty file

    def test_get_isolated_cpus_file_not_found(self, mock_logging):
        """Test behavior when isolated CPUs file doesn't exist."""
        with patch("builtins.open", side_effect=FileNotFoundError("File not found")):
            with pytest.raises(ValueError, match="Failed to read isolated CPUs"):
                get_isolated_cpus()
