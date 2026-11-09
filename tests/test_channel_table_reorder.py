"""
tests/test_channel_table_reorder.py

Tests for rearranging channels in the setup channel table
(`gui/widgets/channel_table.py`):

    - swapping hardware channels: an already assigned channel can be
      picked, and the two rows exchange their hardware channels instead
      of the user having to delete and re-add rows,
    - moving rows up/down,
    - and that the live view display settings stored next to the table
      stay with their channel through all of it (and through renaming,
      which used to drop them).

Real widgets, fake device list - no NI driver needed.
"""

from __future__ import annotations

import unittest

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from data.models import Channel, DeviceInfo, ModuleType, SignalType

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _devices() -> list[DeviceInfo]:
    return [
        DeviceInfo("cDAQ1Mod1", "NI 9215", ModuleType.NI9215, 4, True,
                   [f"cDAQ1Mod1/ai{i}" for i in range(2)]),
        DeviceInfo("cDAQ1Mod2", "NI 9234", ModuleType.NI9234, 4, True,
                   [f"cDAQ1Mod2/ai{i}" for i in range(2)]),
    ]


def _table(channels: list[Channel]):
    from gui.widgets.channel_table import ChannelTableWidget

    _app()
    table = ChannelTableWidget()
    table.set_available_devices(_devices())
    table.set_channels(channels)
    return table


def _all_assigned() -> list[Channel]:
    return [
        Channel("cDAQ1Mod1/ai0", "Kraft", unit="N", module_type=ModuleType.NI9215,
                plot_color="#111111"),
        Channel("cDAQ1Mod1/ai1", "Weg", unit="mm", module_type=ModuleType.NI9215,
                plot_color="#222222"),
        Channel("cDAQ1Mod2/ai0", "Acc", unit="g", module_type=ModuleType.NI9234,
                signal_type=SignalType.IEPE_ACCELERATION, plot_color="#333333"),
        Channel("cDAQ1Mod2/ai1", "Mic", unit="Pa", module_type=ModuleType.NI9234,
                plot_color="#444444"),
    ]


class SwapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = _table(_all_assigned())

    def tearDown(self) -> None:
        self.table.deleteLater()
        _app().processEvents()

    def test_picking_an_assigned_channel_swaps_the_two_rows(self) -> None:
        self.table.assign_hw_channel(0, "cDAQ1Mod1/ai1")
        channels = self.table.get_channels()
        self.assertEqual(
            [(c.display_name, c.hardware_channel) for c in channels[:2]],
            [("Kraft", "cDAQ1Mod1/ai1"), ("Weg", "cDAQ1Mod1/ai0")],
        )
        # Name, unit and display settings stay with the row.
        self.assertEqual([c.unit for c in channels[:2]], ["N", "mm"])
        self.assertEqual([c.plot_color for c in channels[:2]], ["#111111", "#222222"])

    def test_swap_across_modules_rederives_module_and_signal_type(self) -> None:
        self.table.assign_hw_channel(0, "cDAQ1Mod2/ai0")
        kraft, _weg, acc, _mic = self.table.get_channels()
        self.assertEqual((kraft.hardware_channel, kraft.module_type), ("cDAQ1Mod2/ai0", ModuleType.NI9234))
        self.assertEqual((acc.hardware_channel, acc.module_type), ("cDAQ1Mod1/ai0", ModuleType.NI9215))
        # IEPE is not possible on an NI9215 - reset like any module change.
        self.assertEqual(acc.signal_type, SignalType.VOLTAGE)

    def test_swap_without_discovered_hardware_keeps_modules_with_channels(self) -> None:
        from gui.widgets.channel_table import ChannelTableWidget

        table = ChannelTableWidget()
        table.set_channels(_all_assigned())
        table.assign_hw_channel(0, "cDAQ1Mod2/ai0")
        kraft, _weg, acc, _mic = table.get_channels()
        self.assertEqual(kraft.module_type, ModuleType.NI9234)
        self.assertEqual(acc.module_type, ModuleType.NI9215)
        table.deleteLater()

    def test_free_channel_is_just_assigned(self) -> None:
        table = _table(_all_assigned()[:3])
        table.assign_hw_channel(0, "cDAQ1Mod2/ai1")
        self.assertEqual(
            [c.hardware_channel for c in table.get_channels()],
            ["cDAQ1Mod2/ai1", "cDAQ1Mod1/ai1", "cDAQ1Mod2/ai0"],
        )
        table.deleteLater()

    def test_picker_offers_assigned_channels_for_swapping(self) -> None:
        from gui.widgets.channel_table import HardwareChannelPickerDialog, _ROLE_CHANNEL_VALUE

        dialog = HardwareChannelPickerDialog(
            _devices(), {"cDAQ1Mod1/ai1": "Weg"}, "cDAQ1Mod1/ai0"
        )
        items = {
            item.data(0, _ROLE_CHANNEL_VALUE): item
            for top in range(dialog._tree.topLevelItemCount())
            for item in [dialog._tree.topLevelItem(top).child(i)
                         for i in range(dialog._tree.topLevelItem(top).childCount())]
        }
        used = items["cDAQ1Mod1/ai1"]
        self.assertTrue(used.flags() & Qt.ItemFlag.ItemIsSelectable)
        self.assertIn("Weg", used.text(0))
        dialog.deleteLater()

    def test_picker_with_a_plain_set_keeps_the_old_behavior(self) -> None:
        from gui.widgets.channel_table import HardwareChannelPickerDialog, _ROLE_CHANNEL_VALUE

        dialog = HardwareChannelPickerDialog(_devices(), {"cDAQ1Mod1/ai1"}, "cDAQ1Mod1/ai0")
        device = dialog._tree.topLevelItem(0)
        used = next(
            device.child(i) for i in range(device.childCount())
            if device.child(i).data(0, _ROLE_CHANNEL_VALUE) == "cDAQ1Mod1/ai1"
        )
        self.assertFalse(used.flags() & Qt.ItemFlag.ItemIsSelectable)
        dialog.deleteLater()


class MoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = _table(_all_assigned())

    def tearDown(self) -> None:
        self.table.deleteLater()
        _app().processEvents()

    def _names(self) -> list[str]:
        return [c.display_name for c in self.table.get_channels()]

    def _select(self, *rows: int) -> None:
        self.table._table.clearSelection()
        for row in rows:
            self.table._table.selectionModel().select(
                self.table._table.model().index(row, 0),
                self.table._table.selectionModel().SelectionFlag.Select
                | self.table._table.selectionModel().SelectionFlag.Rows,
            )

    def _selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table._table.selectedIndexes()})

    def test_move_down_and_up(self) -> None:
        self._select(0)
        self.table._move_selected_rows(1)
        self.assertEqual(self._names(), ["Weg", "Kraft", "Acc", "Mic"])
        self.assertEqual(self._selected_rows(), [1])
        # The row stays selected - repeated clicks keep moving it.
        self.table._move_selected_rows(1)
        self.assertEqual(self._names(), ["Weg", "Acc", "Kraft", "Mic"])
        self.table._move_selected_rows(-1)
        self.assertEqual(self._names(), ["Weg", "Kraft", "Acc", "Mic"])

    def test_everything_moves_with_the_row(self) -> None:
        self._select(2)
        self.table._move_selected_rows(-1)
        moved = self.table.get_channels()[1]
        self.assertEqual(
            (moved.display_name, moved.hardware_channel, moved.signal_type, moved.plot_color),
            ("Acc", "cDAQ1Mod2/ai0", SignalType.IEPE_ACCELERATION, "#333333"),
        )
        # Row numbers are renumbered.
        self.assertEqual(self.table._table.item(1, 0).text(), "2")

    def test_several_rows_keep_their_order(self) -> None:
        self._select(1, 2)
        self.table._move_selected_rows(1)
        self.assertEqual(self._names(), ["Kraft", "Mic", "Weg", "Acc"])
        self.assertEqual(self._selected_rows(), [2, 3])

    def test_no_move_past_the_edge(self) -> None:
        self._select(0)
        self.table._move_selected_rows(-1)
        self.assertEqual(self._names(), ["Kraft", "Weg", "Acc", "Mic"])
        self._select(3)
        self.table._move_selected_rows(1)
        self.assertEqual(self._names(), ["Kraft", "Weg", "Acc", "Mic"])


class RenameTest(unittest.TestCase):
    def test_renaming_keeps_the_display_settings(self) -> None:
        table = _table(_all_assigned())
        edit = table._table.cellWidget(0, 3)
        edit.setText("Kraft neu")
        edit.editingFinished.emit()
        channel = table.get_channels()[0]
        self.assertEqual((channel.display_name, channel.plot_color), ("Kraft neu", "#111111"))
        table.deleteLater()


if __name__ == "__main__":
    unittest.main()
